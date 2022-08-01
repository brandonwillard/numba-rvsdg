import dis
from copy import deepcopy
from collections import deque, ChainMap, defaultdict
from typing import Optional, Set, Tuple, Dict, List, Sequence
from pprint import pprint
from dataclasses import dataclass, field, replace
import logging

_logger = logging.getLogger(__name__)


class _LogWrap:
    def __init__(self, fn):
        self._fn = fn

    def __str__(self):
        return self._fn()


@dataclass(frozen=True, order=True)
class Label:
    ...


@dataclass(frozen=True, order=True)
class BCLabel(Label):
    offset: int


@dataclass(frozen=True, order=True)
class ControlLabel(Label):
    index: int


class ControlLabelGenerator():

    def __init__(self):
        self.index = 0

    def new_index(self):
        ret = self.index
        self.index += 1
        return ret


clg = ControlLabelGenerator()


@dataclass(frozen=True)
class BasicBlock:
    begin: Label
    """The starting bytecode offset.
    """

    end: Label
    """The bytecode offset immediate after the last bytecode of the block.
    """

    fallthrough: bool
    """Set to True when the block has no terminator. The control should just
    fallthrough to the next block.
    """

    jump_targets: Tuple[Label, ...]
    """The destination block offsets."""

    backedges: Tuple[Label, ...]
    """Backedges offsets"""

    def is_exiting(self) -> bool:
        return not self.jump_targets

    def get_instructions(
        self, bcmap: Dict[Label, dis.Instruction]
    ) -> List[dis.Instruction]:
        begin = self.begin
        end = self.end
        it = begin
        out = []
        while it < end:
            out.append(bcmap[it])
            # increment
            it = _next_inst_offset(it)
        return out

    def replace_backedge(self, loop_head: Label) -> "BasicBlock":
        if loop_head in self.jump_targets:
            assert len(self.jump_targets) == 1
            assert not self.backedges
            return replace(self, jump_targets=(), backedges=(loop_head,))
        return self

    def replace_jump_targets(self, jump_targets: Tuple) -> "BasicBlock":
        return replace(self, jump_targets=jump_targets)


@dataclass(frozen=True)
class RegionBlock(BasicBlock):
    kind: str
    headers: Dict[Label, BasicBlock]
    """The header of the region"""
    subregion: "BlockMap"
    """The subgraph excluding the headers
    """
    exit: Label
    """The exit node.
    """

    def get_full_graph(self):
        graph = ChainMap(self.subregion.graph, self.headers)
        return graph


_cond_jump = {
    "FOR_ITER",
    "POP_JUMP_IF_FALSE",
    "POP_JUMP_IF_TRUE",
}
_uncond_jump = {"JUMP_ABSOLUTE", "JUMP_FORWARD"}
_terminating = {"RETURN_VALUE"}


def is_conditional_jump(opname: str) -> bool:
    return opname in _cond_jump


def is_unconditional_jump(opname: str) -> bool:
    return opname in _uncond_jump


def is_exiting(opname: str) -> bool:
    return opname in _terminating


def _next_inst_offset(offset: Label) -> BCLabel:
    # Fix offset
    assert isinstance(offset, BCLabel)
    return BCLabel(offset.offset + 2)


def _prev_inst_offset(offset: Label) -> BCLabel:
    # Fix offset
    assert isinstance(offset, BCLabel)
    return BCLabel(offset.offset - 2)


@dataclass()
class FlowInfo:
    """ FlowInfo converts Bytecode into a ByteFlow object (CFG). """

    block_offsets: Set[Label] = field(default_factory=set)
    """Marks starting offset of basic-block
    """

    jump_insts: Dict[Label, Tuple[Label, ...]] = field(default_factory=dict)
    """Contains jump instructions and their target offsets.
    """

    last_offset: int = field(default=0)
    """Offset of the last bytecode instruction.
    """

    def _add_jump_inst(self, offset: Label, targets: Sequence[Label]):
        """Add jump instruction to FlowInfo."""
        for off in targets:
            assert isinstance(off, Label)
            self.block_offsets.add(off)
        self.jump_insts[offset] = tuple(targets)

    @staticmethod
    def from_bytecode(bc: dis.Bytecode) -> "FlowInfo":
        """
        Build control-flow information that marks start of basic-blocks and
        jump instructions.
        """
        flowinfo = FlowInfo()

        for inst in bc:
            # Handle jump-target instruction
            if inst.offset == 0 or inst.is_jump_target:
                flowinfo.block_offsets.add(BCLabel(inst.offset))
            # Handle by op
            if is_conditional_jump(inst.opname):
                flowinfo._add_jump_inst(
                    BCLabel(inst.offset),
                    (BCLabel(inst.argval),
                     _next_inst_offset(BCLabel(inst.offset))),
                )
            elif is_unconditional_jump(inst.opname):
                flowinfo._add_jump_inst(BCLabel(inst.offset),
                                        (BCLabel(inst.argval),))
            elif is_exiting(inst.opname):
                flowinfo._add_jump_inst(BCLabel(inst.offset), ())

        flowinfo.last_offset = inst.offset
        return flowinfo

    def build_basicblocks(self: "FlowInfo", end_offset=None) -> "BlockMap":
        """
        Build a graph of basic-blocks
        """
        offsets = sorted(self.block_offsets)
        if end_offset is None:
            end_offset = _next_inst_offset(BCLabel(self.last_offset))
        bbmap = BlockMap()
        for begin, end in zip(offsets, [*offsets[1:], end_offset]):
            targets: Tuple[Label, ...]
            term_offset = _prev_inst_offset(end)
            if term_offset not in self.jump_insts:
                # implicit jump
                targets = (end,)
                fallthrough = True
            else:
                targets = self.jump_insts[term_offset]
                fallthrough = False
            bb = BasicBlock(begin=begin,
                            end=end,
                            jump_targets=targets,
                            fallthrough=fallthrough,
                            backedges=(),
                            )
            bbmap.add_node(bb)
        return bbmap


@dataclass(frozen=True)
class BlockMap:
    """ Map of Labels to Blocks. """
    graph: Dict[Label, BasicBlock] = field(default_factory=dict)

    def add_node(self, basicblock: BasicBlock):
        self.graph[basicblock.begin] = basicblock

    def exclude_nodes(self, exclude_nodes: Set[Label]):
        """Iterator over all nodes not in exclude_nodes. """
        for node in self.graph:
            if node not in exclude_nodes:
                yield node

    def find_head(self):
        heads = set(self.graph.keys())
        for block in self.graph.keys():
            for jt in self.graph[block].jump_targets:
                heads.discard(jt)
        assert len(heads) == 1
        return next(iter(heads))

    def compute_scc(self) -> List[Set[Label]]:
        """
        Strongly-connected component for detecting loops.
        """
        from scc import scc

        class GraphWrap:
            def __init__(self, graph):
                self.graph = graph

            def __getitem__(self, vertex):
                out = self.graph[vertex].jump_targets
                # Exclude node outside of the subgraph
                return [k for k in out if k in self.graph]

            def __iter__(self):
                return iter(self.graph.keys())

        return list(scc(GraphWrap(self.graph)))

    def find_headers_and_entries(self, loop: Set[Label]):
        """Find entried and headers in a given loop.

        Entries are nodes outside the loop that have an edge pointing to the
        loop header. Headers are nodes that are part of the strongly connected
        subset, that have incoming edges from outside the loop entries point to
        headers and headers are pointed to by entries.

        """
        node: Label
        entries: Set[Label] = set()
        headers: Set[Label] = set()

        for node in self.exclude_nodes(loop):
            nodes_jump_in_loop = set(self.graph[node].jump_targets) & loop
            headers |= nodes_jump_in_loop
            if nodes_jump_in_loop:
                entries.add(node)

        return headers, entries

    def join_returns(self):
        """ Close the CFG.

        A closed CFG is a CFG with a unique entry and exit node that have no
        predescessors and no successors respectively.
        """

        return_nodes = [node for node in self.graph
                        if self.graph[node].is_exiting()]
        if len(return_nodes) > 1:
            return_solo_label = ControlLabel(clg.new_index())
            return_solo_block = BasicBlock(
                begin=return_solo_label,
                end=ControlLabel(clg.new_index()),
                fallthrough=False,
                jump_targets=tuple(),
                backedges=tuple()
                )
            self.add_node(return_solo_block)
            for rnode in return_nodes:
                self.add_node(self.graph.pop(rnode).replace_jump_targets(
                            jump_targets=(return_solo_label,)))


@dataclass(frozen=True)
class ByteFlow:
    bc: dis.Bytecode
    bbmap: "BlockMap"

    @staticmethod
    def from_bytecode(code) -> "ByteFlow":
        bc = dis.Bytecode(code)
        _logger.debug("Bytecode\n%s", _LogWrap(lambda: bc.dis()))

        flowinfo = FlowInfo.from_bytecode(bc)
        bbmap = flowinfo.build_basicblocks()
        return ByteFlow(bc=bc, bbmap=bbmap)

    def _join_returns(self):
        bbmap = deepcopy(self.bbmap)
        self.bbmap.join_returns()
        return ByteFlow(bc=self.bc, bbmap=bbmap)

    def _restructure_loop(self):
        bbmap = deepcopy(self.bbmap)
        restructure_loop(bbmap)
        return ByteFlow(bc=self.bc, bbmap=bbmap)

    def _restructure_branch(self):
        bbmap = deepcopy(self.bbmap)
        restructure_branch(bbmap)
        return ByteFlow(bc=self.bc, bbmap=bbmap)

    def restructure(self):
        bbmap = deepcopy(self.bbmap)
        # close
        self.bbmap.join_returns()
        # handle loop
        restructure_loop(bbmap)
        # handle branch
        restructure_branch(bbmap)
        #for region in _iter_subregions(bbmap):
        #    restructure_branch(region.subregion)
        return ByteFlow(bc=self.bc, bbmap=bbmap)


def _iter_subregions(bbmap: "BlockMap"):
    for node in bbmap.graph.values():
        if isinstance(node, RegionBlock):
            yield node
            yield from _iter_subregions(node.subregion)


def find_exits(loop: Set[Label], bbmap: BlockMap):
    """Find exits in a given loop.

    Exits are nodes outside the loop that have incoming edges from within the
    loop.
    """
    node: Label
    pre_exits: Set[Label] = set()
    post_exits: Set[Label] = set()
    for inside in loop:
        # any node inside that points outside the loop
        for jt in bbmap.graph[inside].jump_targets:
            if jt not in loop:
                pre_exits.add(inside)
                post_exits.add(jt)
        # any returns
        if bbmap.graph[inside].is_exiting():
            pre_exits.add(inside)
    return pre_exits, post_exits


def join_exits(loop: Set[Label], bbmap: BlockMap, exits: Set[Label]):
    # create a single exit label and add it to the loop
    pre_exit_label = ControlLabel(clg.new_index())
    post_exit_label = ControlLabel(clg.new_index())
    loop.add(pre_exit_label)
    # create the exit block and add it to the block map
    post_exit_block = BasicBlock(begin=post_exit_label,
                                 end=ControlLabel(clg.new_index()),
                                 fallthrough=False,
                                 jump_targets=tuple(exits),
                                 backedges=tuple()
                                 )
    pre_exit_block = BasicBlock(begin=pre_exit_label,
                                end=ControlLabel(clg.new_index()),
                                fallthrough=False,
                                jump_targets=(post_exit_label,),
                                backedges=tuple()
                                )
    bbmap.add_node(pre_exit_block)
    bbmap.add_node(post_exit_block)
    # for all exits, find the nodes that jump to this exit
    # this is effectively finding the exit vertices
    for exit_node in exits:
        for loop_node in loop:
            if loop_node == pre_exit_label:
                continue
            if exit_node in bbmap.graph[loop_node].jump_targets:
                # update the jump_targets to point to the new exitnode
                # by replacing the original node with updates
                new_jump_targets = tuple(
                    [t for t in bbmap.graph[loop_node].jump_targets
                     if t != exit_node]
                    + [pre_exit_label])
                bbmap.add_node(
                    bbmap.graph.pop(loop_node).replace_jump_targets(
                        jump_targets=new_jump_targets))
    return pre_exit_label, post_exit_label


def join_pre_exits(exits: Set[Label], post_exit: Label,
                   inner_nodes: Set[Label], bbmap: BlockMap, ):
    pre_exit_label = ControlLabel(clg.new_index())
    # create the exit block and add it to the block map
    pre_exit_block = BasicBlock(begin=pre_exit_label,
                                end=ControlLabel(clg.new_index()),
                                fallthrough=False,
                                jump_targets=(post_exit,),
                                backedges=tuple()
                                )
    bbmap.add_node(pre_exit_block)
    inner_nodes.add(pre_exit_label)
    # for all exits, find the nodes that jump to this exit
    # this is effectively finding the exit vertices
    for exit_node in exits:
        bbmap.add_node(
            bbmap.graph.pop(exit_node).replace_jump_targets(
                jump_targets=(pre_exit_label,)))
    return pre_exit_label


def join_headers(headers, entries, bbmap):
    assert len(headers) > 1
    # create the synthetic entry block
    synth_entry_label = ControlLabel(clg.new_index())
    synth_entry_block = BasicBlock(begin=synth_entry_label,
                                   end=ControlLabel(clg.new_index()),
                                   fallthrough=False,
                                   jump_targets=tuple(headers),
                                   backedges=tuple()
                                   )
    bbmap.add_node(synth_entry_block)
    # rewire headers
    for block in entries:
        bbmap.add_node(bbmap.graph.pop(block).replace_jump_targets(
                        jump_targets=(synth_entry_label,)))
    return synth_entry_label, synth_entry_block


def is_reachable_dfs(bbmap, begin, end):
    """Is end reachable from begin. """
    seen = set()
    to_vist = list(bbmap.graph[begin].jump_targets)
    while True:
        if to_vist:
            block = to_vist.pop()
        else:
            return False

        if block in seen:
            continue
        elif block == end:
            return True
        elif block not in seen:
            seen.add(block)
            if block in bbmap.graph:
                to_vist.extend(bbmap.graph[block].jump_targets)


def restructure_loop(bbmap: BlockMap):
    """Inplace restructuring of the given graph to extract loops using
    strongly-connected components
    """
    # obtain a List of Sets of Labels, where all labels in each set are strongly
    # connected, i.e. all reachable from one another by traversing the subset
    scc: List[Set[Label]] = bbmap.compute_scc()
    # loops are defined as strongly connected subsets who have more than a
    # single label
    loops: List[Set[Label]] = [nodes for nodes in scc if len(nodes) > 1]
    _logger.debug("restructure_loop found %d loops in %s",
                  len(loops), bbmap.graph.keys())

    # extract loop
    for loop in loops:
        _logger.debug("loop nodes %s", loop)

        headers, entries = bbmap.find_headers_and_entries(loop)
        _logger.debug("loop headers %s", headers)
        _logger.debug("loop entries %s", entries)

        pre_exits, post_exits = find_exits(loop, bbmap)
        _logger.debug("loop pre exits %s", pre_exits)
        _logger.debug("loop post exits %s", post_exits)

        if len(post_exits) != 1:
            pre_exit_label, post_exit_label = join_exits(loop,
                                                         bbmap,
                                                         post_exits)
        elif len(pre_exits) != 1:
            Exception("unreachable?")
        else:
            pre_exit_label, post_exit_label = (next(iter(pre_exits)),
                                               next(iter(post_exits)))
        _logger.debug("loop pre_exit_label %s", pre_exit_label)
        _logger.debug("loop post_exit_label %s", post_exit_label)

        # remove loop nodes from cfg/bbmap
        # use the set of labels to remove/pop Blocks into a set of blocks
        insiders: Set[BasicBlock] = {bbmap.graph.pop(k) for k in loop}

        assert len(headers) == 1, headers  # TODO join entries

        # turn singleton set into single element
        loop_head: Label = next(iter(headers))
        # construct the loop body, identifying backedges as we go
        loop_body: Dict[Label, BasicBlock] = {
            node.begin: node.replace_backedge(loop_head)
            for node in insiders if node.begin not in headers}
        # create a subregion
        blk = RegionBlock(
            begin=loop_head,
            end=_next_inst_offset(loop_head),
            fallthrough=False,
            jump_targets=(post_exit_label,),
            backedges=(),
            kind="loop",
            subregion=BlockMap(loop_body),
            headers={node.begin: node for node in insiders
                     if node.begin in headers},
            exit=pre_exit_label
        )
        # process subregions
        restructure_loop(blk.subregion)
        # insert subregion back into original
        bbmap.graph[loop_head] = blk


def restructure_branch(bbmap: BlockMap):
    print("restructure_branch", bbmap.graph)
    doms = _doms(bbmap)
    postdoms = _post_doms(bbmap)
    postimmdoms = _imm_doms(postdoms)
    immdoms = _imm_doms(doms)
    recursive_subregions = []
    # find head block and check that it is unique
    for begin, end in _iter_branch_regions(bbmap, immdoms, postimmdoms):
        _logger.debug("branch region: %s -> %s", begin, end)
        # find exiting nodes from branch
        # exits = {k for k, node in bbmap.graph.items()
        #          if begin <= k < end and end in node.jump_targets}
        # partition the branches

        # partition head
        head = bbmap.find_head()
        head_subregion = []
        current_block = head
        while True:
            head_subregion.append(current_block)
            if current_block == begin:
                break
            else:
                current_block = bbmap.graph[current_block].jump_targets[0]

        subgraph = BlockMap()
        for block in head_subregion:
            subgraph.add_node(bbmap.graph[block])
        subregion = RegionBlock(
            begin=head,
            end=begin,
            fallthrough=False,
            jump_targets=bbmap.graph[begin].jump_targets,
            backedges=(),
            kind="head",
            headers=(head,),
            subregion=subgraph,
            exit=None,
        )
        for block in head_subregion:
            del bbmap.graph[block]
        bbmap.graph[begin] = subregion

        # insert synthetic branch blocks in case of empty branch regions
        jump_targets = list(bbmap.graph[begin].jump_targets)
        new_jump_targets = []
        jump_targets_changed = False
        for a in jump_targets:
            for b in jump_targets:
                if a == b:
                    continue
                elif is_reachable_dfs(bbmap, a, b):
                    # If one of the jump targets is reachable from the other,
                    # it means a branch region is empty and the reachable jump
                    # target becoms part of the tail. In this case,
                    # create a new snythtic block to fill the empty branch
                    # region and rewire the jump targets.
                    synthetic_branch_block_label = ControlLabel(clg.new_index())
                    synthetic_branch_block_block = BasicBlock(
                            begin=synthetic_branch_block_label,
                            end=ControlLabel(clg.new_index()),
                            jump_targets=(b,),
                            fallthrough=True,
                            backedges=(),
                            )
                    bbmap.add_node(synthetic_branch_block_block)
                    new_jump_targets.append(synthetic_branch_block_label)
                    jump_targets_changed = True
                else:
                    # otherwise just re-use the existing block
                    new_jump_targets.append(b)

        # update the begin block with new jump_targets
        if jump_targets_changed:
            bbmap.add_node(
                bbmap.graph.pop(begin).replace_jump_targets(
                    jump_targets=new_jump_targets))
            # and recompute doms
            doms = _doms(bbmap)
            postdoms = _post_doms(bbmap)
            postimmdoms = _imm_doms(postdoms)
            immdoms = _imm_doms(doms)

        # identify branch regions
        branch_regions = []
        for bra_start in bbmap.graph[begin].jump_targets:
            sub_keys: Set[BCLabel] = set()
            branch_regions.append((bra_start, sub_keys))
            # a node is part of the branch if
            # - the start of the branch is a dominator; and,
            # - the tail of the branch is not a dominator
            for k, kdom in doms.items():
                if bra_start in kdom and end not in kdom:
                    sub_keys.add(k)

        # identify and close tail
        #
        # find all nodes that are left
        tail_subregion = set((b for b in bbmap.graph.keys()))
        for b, sub in branch_regions:
            tail_subregion.discard(b)
            for s in sub:
                tail_subregion.discard(s)
        # exclude parents
        tail_subregion.discard(begin)

        headers, entries = bbmap.find_headers_and_entries(tail_subregion)
        exits, _ = find_exits(tail_subregion, bbmap)
        #if len(returns) == 0:
        #    returns = set([block for block in tail_subregion if bbmap.graph[block].])

        #assert len(returns) == 1

        if len(headers) > 1:
            entry_label, entry_block = join_headers(
                headers, entries, bbmap)
            tail_subregion.add(entry_label)
        else:
            entry_label = next(iter(headers))

        # end of the graph
        #assert len(pre_exits) == 1
        #assert len(post_exits) == 0

        subgraph = BlockMap()
        for block in tail_subregion:
            subgraph.add_node(bbmap.graph[block])
        subregion = RegionBlock(
            begin=entry_label,
            end=next(iter(exits)),
            fallthrough=False,
            jump_targets=(),
            backedges=(),
            kind="tail",
            headers=headers,
            subregion=subgraph,
            exit=None,
        )
        for block in tail_subregion:
            del bbmap.graph[block]
        bbmap.graph[entry_label] = subregion

        if subregion.subregion.graph:
            recursive_subregions.append(subregion.subregion)

        # extract the subregion
        for bra_start, inner_nodes in branch_regions:
            if inner_nodes:  # and len(inner_nodes) > 1:
                pre_exits, post_exits = find_exits(inner_nodes, bbmap)
                if len(pre_exits) != 1 and len(post_exits) == 1:
                    post_exit_label = next(iter(post_exits))
                    pre_exit_label = join_pre_exits(
                        pre_exits,
                        post_exit_label,
                        inner_nodes,
                        bbmap)
                elif len(pre_exits) != 1 and len(post_exits) > 1:
                    pre_exit_label, post_exit_label = join_exits(
                        inner_nodes,
                        bbmap,
                        post_exits)
                else:
                    pre_exit_label = next(iter(pre_exits))
                    post_exit_label = bbmap.graph[pre_exit_label].jump_targets[0]

                subgraph = BlockMap()
                for k in inner_nodes:
                    subgraph.add_node(bbmap.graph[k])

                subregion = RegionBlock(
                    begin=bra_start,
                    end=end,
                    fallthrough=False,
                    jump_targets=(post_exit_label,),
                    backedges=(),
                    kind="branch",
                    headers={},
                    subregion=subgraph,
                    exit=pre_exit_label,
                )
                for k in inner_nodes:
                    del bbmap.graph[k]
                bbmap.graph[bra_start] = subregion

                if subregion.subregion.graph:
                    recursive_subregions.append(subregion.subregion)

    # recurse into subregions as necessary
    for region in recursive_subregions:
        restructure_branch(region)


def _iter_branch_regions(bbmap: BlockMap,
                         immdoms: Dict[Label, Label],
                         postimmdoms: Dict[Label, Label]):
    for begin, node in [i for i in bbmap.graph.items()]:
        if len(node.jump_targets) > 1:
            # found branch
            if begin in postimmdoms:
                end = postimmdoms[begin]
                if immdoms[end] == begin:
                    yield begin, end


def _imm_doms(doms: Dict[Label, Set[Label]]) -> Dict[Label, Label]:
    idoms = {k: v - {k} for k, v in doms.items()}
    changed = True
    while changed:
        changed = False
        for k, vs in idoms.items():
            nstart = len(vs)
            for v in list(vs):
                vs -= idoms[v]
            if len(vs) < nstart:
                changed = True
    # fix output
    out = {}
    for k, vs in idoms.items():
        if vs:
            [v] = vs
            out[k] = v
    return out


def _doms(bbmap: BlockMap):
    # compute dom
    entries = set()
    preds_table = defaultdict(set)
    succs_table = defaultdict(set)

    node: BasicBlock
    for src, node in bbmap.graph.items():
        for dst in node.jump_targets:
            # check dst is in subgraph
            if dst in bbmap.graph:
                preds_table[dst].add(src)
                succs_table[src].add(dst)

    for k in bbmap.graph:
        if not preds_table[k]:
            entries.add(k)
    return _find_dominators_internal(entries, list(bbmap.graph.keys()), preds_table, succs_table)


def _post_doms(bbmap: BlockMap):
    # compute post dom
    entries = set()
    for k, v in bbmap.graph.items():
        targets = set(v.jump_targets) & set(bbmap.graph)
        if not targets:
            entries.add(k)
    preds_table = defaultdict(set)
    succs_table = defaultdict(set)

    node: BasicBlock
    for src, node in bbmap.graph.items():
        for dst in node.jump_targets:
            # check dst is in subgraph
            if dst in bbmap.graph:
                preds_table[src].add(dst)
                succs_table[dst].add(src)

    return _find_dominators_internal(entries, list(bbmap.graph.keys()), preds_table, succs_table)


def _find_dominators_internal(entries, nodes, preds_table, succs_table):
    # From NUMBA
    # See theoretical description in
    # http://en.wikipedia.org/wiki/Dominator_%28graph_theory%29
    # The algorithm implemented here uses a todo-list as described
    # in http://pages.cs.wisc.edu/~fischer/cs701.f08/finding.loops.html

    # if post:
    #     entries = set(self._exit_points)
    #     preds_table = self._succs
    #     succs_table = self._preds
    # else:
    #     entries = set([self._entry_point])
    #     preds_table = self._preds
    #     succs_table = self._succs

    import functools

    if not entries:
        raise RuntimeError("no entry points: dominator algorithm "
                           "cannot be seeded")

    doms = {}
    for e in entries:
        doms[e] = set([e])

    todo = []
    for n in nodes:
        if n not in entries:
            doms[n] = set(nodes)
            todo.append(n)

    while todo:
        n = todo.pop()
        if n in entries:
            continue
        new_doms = set([n])
        preds = preds_table[n]
        if preds:
            new_doms |= functools.reduce(set.intersection,
                                         [doms[p] for p in preds])
        if new_doms != doms[n]:
            assert len(new_doms) < len(doms[n])
            doms[n] = new_doms
            todo.extend(succs_table[n])
    return doms


class ByteFlowRenderer(object):

    def __init__(self):
        from graphviz import Digraph
        self.g = Digraph()

    def render_region_block(self, digraph: "Digraph", label: Label, regionblock: RegionBlock):
        # render subgraph
        graph = regionblock.get_full_graph()
        with digraph.subgraph(name=f"cluster_{label}") as subg:
            color = 'blue'
            if regionblock.kind == 'branch':
                color = 'green'
            if regionblock.kind == 'tail':
                color = 'purple'
            if regionblock.kind == 'head':
                color = 'red'
            subg.attr(color=color, label=regionblock.kind)
            for label, block in graph.items():
                self.render_block(subg, label, block)
        # render edges within this region
        self.render_edges(graph)

    def render_basic_block(self, digraph: "Digraph", label: Label, block: BasicBlock):
        if isinstance(label, BCLabel):
            instlist = block.get_instructions(self.bcmap)
            body = "\l".join(
                [f"{inst.offset:3}: {inst.opname}" for inst in instlist] + [""]
            )
        elif isinstance(label, ControlLabel):
            body = "Control Label: " + str(label.index)
        digraph.node(str(label), shape="rect", label=body)

    def render_block(self, digraph: "Digraph", label: Label, block: BasicBlock):
        if type(block) == BasicBlock:
            self.render_basic_block(digraph, label, block)
        elif type(block) == RegionBlock:
            self.render_region_block(digraph, label, block)
        else:
            raise Exception("unreachable")

    def render_edges(self, blocks: Dict[Label, BasicBlock]):
        for label, block in blocks.items():
            for dst in block.jump_targets:
                if dst in blocks:
                    if type(block) == BasicBlock:
                        self.g.edge(str(label), str(dst))
                    elif type(block) == RegionBlock:
                        if block.exit is not None:
                            self.g.edge(str(block.exit), str(dst))
                        else:
                            self.g.edge(str(label), str(dst))
                    else:
                        raise Exception("unreachable")
            for dst in block.backedges:
                assert dst in blocks
                self.g.edge(str(label), str(dst),
                            style="dashed", color="grey", constraint="0")

    def render_byteflow(self, byteflow: ByteFlow):
        self.bcmap_from_bytecode(byteflow.bc)

        # render nodes
        for label, block in byteflow.bbmap.graph.items():
            self.render_block(self.g, label, block)
        self.render_edges(byteflow.bbmap.graph)
        return self.g

    def bcmap_from_bytecode(self, bc: dis.Bytecode):
        self.bcmap: Dict[Label, dis.Instruction] = {BCLabel(inst.offset): inst
                                                    for inst in bc}

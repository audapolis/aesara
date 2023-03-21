import traceback
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)
from typing import cast as type_cast
from warnings import warn

import numpy as np

import aesara
from aesara.configdefaults import config
from aesara.graph.basic import Constant, Variable, ancestors, equal_computations
from aesara.graph.features import AlreadyThere, Feature
from aesara.graph.fg import FunctionGraph
from aesara.graph.rewriting.basic import (
    GraphRewriter,
    check_chain,
    copy_stack_trace,
    node_rewriter,
)
from aesara.graph.utils import InconsistencyError
from aesara.tensor.basic import (
    MakeVector,
    as_tensor_variable,
    cast,
    constant,
    extract_constant,
    get_scalar_constant_value,
    stack,
)
from aesara.tensor.elemwise import DimShuffle, Elemwise
from aesara.tensor.exceptions import NotScalarConstantError, ShapeError
from aesara.tensor.rewriting.basic import (
    register_canonicalize,
    register_specialize,
    register_stabilize,
    register_useless,
    topo_constant_folding,
)
from aesara.tensor.shape import (
    Reshape,
    Shape,
    Shape_i,
    SpecifyShape,
    Unbroadcast,
    shape_i,
    specify_shape,
    unbroadcast,
)
from aesara.tensor.subtensor import Subtensor, get_idx_list
from aesara.tensor.type import HasShape, TensorType, discrete_dtypes, integer_dtypes
from aesara.tensor.type_other import NoneConst


if TYPE_CHECKING:
    from numpy.typing import ArrayLike

    from aesara.graph.basic import Apply
    from aesara.graph.tensor.var import TensorVariable

    InputShapesType = List[Optional[Tuple[Variable, ...]]]
    OutputShapesType = List[Optional[Tuple[Variable, ...]]]
    ShapeInferFunctionType = Callable[
        [FunctionGraph, "Apply", InputShapesType], OutputShapesType
    ]


class ShapeFeature(Feature):
    r"""A `Feature` that tracks shape information in a graph.

    This `Feature` aids in the replacement of all `Shape`\s and `Subtensor`\s of `Shape`\s with
    `Shape_i` and `MakeVector` `Op`\s.

    This `Feature` and its associated rewrites have several goals:

    1. to "lift" `Shape`\s to as close to the inputs as possible,
    2. to infer the shape of every node in the graph in terms of the
       input shapes, and
    3. remove fill `Op`\s (e.g. `Second`) from the graph.

    Lifting shapes as close to the inputs as possible is important for
    canonicalization because it is very bad form to have to compute
    something just to know how big it will be.  Firstly, it is a waste
    of time to compute such outputs.  But it is important to get rid
    of these outputs as early as possible in the compilation process
    because the extra computations make it appear as if many internal
    graph nodes have multiple clients.  Many rewrites refuse to
    work on nodes with multiple clients.

    Lifting is done by using an :meth:`Op.infer_shape` method if one is
    present, or else using a conservative default..

    Inferring the shape of internal nodes in the graph is important for doing
    size-driven rewrites.  If we know how big various intermediate results will
    be, we can estimate the cost of many `Op`\s accurately, and generate code
    that is specific (e.g. unrolled) to particular sizes.

    In cases where `ShapeFeature` cannot figure out the shape, it raises a
    `ShapeError`.

    .. note::

        We can't automatically infer the shape of shared variables as they can
        change of shape during the execution by default.

    To use the shape information gathered by a `FunctionGraph`-attached
    `ShapeFeature` in rewrites, use the :meth:`ShapeFeature.get_shape` method.

    """
    lscalar_one = constant(1, dtype="int64", ndim=0)

    def get_node_infer_shape(
        self, fgraph: FunctionGraph, node: "Apply"
    ) -> "OutputShapesType":
        try:
            shape_infer: "ShapeInferFunctionType" = node.op.infer_shape
        except AttributeError:
            shape_infer = self.default_infer_shape

        try:
            o_shapes = shape_infer(
                fgraph, node, [self.shape_of[r] for r in node.inputs]
            )
        except ShapeError:
            o_shapes = self.default_infer_shape(
                fgraph, node, [self.shape_of[r] for r in node.inputs]
            )
        except NotImplementedError as e:
            raise NotImplementedError(
                "Code called by infer_shape failed raising a "
                "NotImplementedError. Raising NotImplementedError to "
                "indicate that a shape cannot be computed is no longer "
                "supported, and one should now use ShapeError "
                f"instead. The original exception message is: {e}"
            ).with_traceback(e.__traceback__)
        except Exception as e:
            msg = (
                f"Failed to infer_shape from Op {node.op}.\nInput shapes: "
                f"{[self.shape_of[r] for r in node.inputs]}\nException encountered during infer_shape: "
                f"{type(e)}\nException message: {str(e)}\nTraceback: {traceback.format_exc()}"
            )
            if config.on_shape_error == "raise":
                raise Exception(msg).with_traceback(e.__traceback__)
            else:
                warn(msg)
            o_shapes = self.default_infer_shape(
                fgraph, node, [self.shape_of[r] for r in node.inputs]
            )

        return o_shapes

    def get_shape(self, fgraph: FunctionGraph, var: Variable, idx: int) -> Variable:
        """Get the shape of `var` at index `idx`.

        It is better to call this than use ``ShapeFeature.shape_of[var][idx]``,
        since this method will update `ShapeFeature.shape_of` when needed.

        TODO: Up to now, we don't update it in all cases. Update in all cases.

        """
        var_shape = self.shape_of[var]
        assert var_shape is not None

        var_idx_shape = var_shape[idx]

        if (
            var_idx_shape.owner
            and isinstance(var_idx_shape.owner.op, Shape_i)
            and var_idx_shape.owner.inputs[0] not in fgraph.variables
        ):
            assert var.owner
            node = var.owner

            # Recurse on inputs
            # TODO FIXME: Remove the recursion here.
            for i in node.inputs:
                if isinstance(i.type, HasShape):
                    self.get_shape(fgraph, i, 0)

            o_shapes = self.get_node_infer_shape(fgraph, node)
            assert len(o_shapes) == len(node.outputs)

            # Only change the variables and dimensions that would introduce
            # extra computation
            for new_shps, out in zip(o_shapes, node.outputs):
                if not isinstance(out.type, HasShape):
                    continue

                out_shape = self.shape_of[out]
                assert out_shape is not None

                merged_shps = list(out_shape)

                changed = False
                for i in range(out.type.ndim):
                    n_r = merged_shps[i]
                    if (
                        n_r.owner
                        and isinstance(n_r.owner.op, Shape_i)
                        and n_r.owner.inputs[0] not in fgraph.variables
                    ):
                        changed = True

                        assert new_shps is not None

                        merged_shps[i] = new_shps[i]

                if changed:
                    self.set_shape(out, merged_shps, override=True)

            var_shape = self.shape_of[var]
            assert var_shape is not None

            var_idx_shape = var_shape[idx]

        return var_idx_shape

    def shape_ir(self, i: int, r: Variable) -> Variable:
        r"""Return symbolic `r.shape[i]`."""
        if isinstance(r.type, HasShape) and r.type.shape[i] is not None:
            return constant(r.type.shape[i], dtype="int64", ndim=0)
        else:
            # Do not call make_node for test_value
            s = Shape_i(i)(r)

            assert isinstance(s, Variable)

            try:
                s = constant(get_scalar_constant_value(s), dtype="int64", ndim=0)
            except NotScalarConstantError:
                pass

            return s

    def shape_tuple(self, r: Variable) -> Optional[Tuple[Variable, ...]]:
        """Return a tuple of symbolic shape vars for tensor variable r."""
        if not isinstance(r.type, HasShape):
            # This happen for NoneConst.
            return None
        return tuple(self.shape_ir(i, r) for i in range(r.type.ndim))

    def default_infer_shape(
        self, fgraph: FunctionGraph, node: "Apply", i_shapes: "InputShapesType"
    ) -> "OutputShapesType":
        """Return a list of shape tuple or None for the outputs of node.

        This function is used for Ops that don't implement infer_shape.
        Ops that do implement infer_shape should use the i_shapes parameter,
        but this default implementation ignores it.

        """
        rval = []
        for r in node.outputs:
            try:
                rval.append(self.shape_tuple(r))
            except AttributeError:
                rval.append(None)
        return rval

    def to_symbolic_int(
        self, s_i: Union[int, float, np.integer, "ArrayLike", Variable]
    ) -> Variable:
        """Return a symbolic integer scalar for the shape element `s_i`.

        TODO: Re-evaluate the need for this, since it's effectively eager
        canonicalization.

        Parameters
        ----------
        s_i
            The `s_i` argument is assumed to be produced by an :meth:`Op.infer_shape`.

        """
        if s_i == 1:
            return self.lscalar_one

        if isinstance(s_i, (float, int, np.integer)) or (
            isinstance(s_i, np.ndarray) and s_i.ndim == 0
        ):
            assert int(s_i) == s_i and s_i >= 0
            return constant(s_i, dtype="int64", ndim=0)

        assert isinstance(s_i, Variable)

        # TODO FIXME: This is eager canonicalization; we should let the
        # relevant canonicalization passes do their job and not perform the
        # same logic manually.
        if (
            s_i.owner
            and isinstance(s_i.owner.op, Subtensor)
            and s_i.owner.inputs[0].owner
            and isinstance(s_i.owner.inputs[0].owner.op, Shape)
        ):
            # s_i is x.shape[i] for some x, we change it to shape_of[x][i]
            assert s_i.type.ndim == 0
            assert len(s_i.owner.op.idx_list) == 1

            # The current Subtensor always put constant index in the graph.
            # This was not True in the past. So call the Subtensor function
            # that will return the right index.
            idx = get_idx_list(s_i.owner.inputs, s_i.owner.op.idx_list)
            assert len(idx) == 1
            idx = idx[0]
            try:
                i = get_scalar_constant_value(idx)
            except NotScalarConstantError:
                return s_i
            else:
                # Executed only if no exception was raised
                x = s_i.owner.inputs[0].owner.inputs[0]
                # x should already have been imported, and should be in shape_of.
                s_x = self.shape_of[x]
                assert s_x is not None
                s_i = s_x[i]

        if s_i.type.dtype not in integer_dtypes or getattr(s_i.type, "ndim", 0) != 0:
            raise TypeError(f"Shape element {str(s_i)} must be an integer scalar")

        return s_i

    def set_shape(
        self, r: Variable, s: Optional[Sequence[Variable]], override: bool = False
    ) -> None:
        """Assign the shape `s` to previously un-shaped variable `r`.

        Parameters
        ----------
        r
        s
        override
            If ``False``, it means `r` is a new, unseen term.
            If ``True``, it means `r` is assumed to have already been seen and
            we want to override its shape.

        """
        if not override:
            assert r not in self.shape_of, "r already in shape_of"
        if s is None:
            self.shape_of[r] = s
        else:
            if not isinstance(s, (tuple, list)):
                raise TypeError("shapes must be tuple/list", (r, s))

            if r.type.ndim != len(s):
                raise AssertionError(
                    f"A shape with {len(s)} dimensions was inferred for {r}: "
                    f"a variable with {int(r.type.ndim)} dimensions."
                )

            shape_vars: Tuple[Variable, ...] = ()
            for i in range(r.type.ndim):
                if isinstance(r.type, HasShape) and r.type.shape[i] is not None:
                    shape_vars += (constant(r.type.shape[i], dtype="int64", ndim=0),)
                else:
                    shape_vars += (self.to_symbolic_int(s[i]),)

            assert all(
                not isinstance(r.type, HasShape)
                or r.type.shape[i] != 1
                or self.lscalar_one.equals(shape_vars[i])
                or self.lscalar_one.equals(extract_constant(shape_vars[i]))
                for i in range(r.type.ndim)
            )

            self.shape_of[r] = tuple(shape_vars)

            for sv in shape_vars:
                self.shape_of_reverse_index.setdefault(sv, set()).add(r)

    def update_shape(self, r: Variable, other_r: Variable) -> None:
        """Replace shape of `r` by shape of `other_r`.

        If, on some dimensions, the shape of `other_r` is not informative, keep
        the shape of `r` on those dimensions.

        """
        # other_r should already have a shape
        assert other_r in self.shape_of, ("other_r not in shape_of", other_r)
        other_shape = self.shape_of[other_r]

        # If other_shape has no information, call is pointless.
        if other_shape is None:
            return

        if r in self.shape_of:
            r_shape = self.shape_of[r]
        else:
            # If no info is known on r's shape, use other_shape
            self.set_shape(r, other_shape)
            return

        if (
            other_r.owner
            and r.owner
            and other_r.owner.inputs == r.owner.inputs
            and other_r.owner.op == r.owner.op
        ):
            # We are doing a merge, so the two shape graphs will be the
            # same.  This is only done so that we call `ancestors` less
            # frequently.
            return

        # Merge other_shape with r_shape, giving the priority to other_shape
        merged_shape: Tuple[Variable, ...] = ()
        for i, ps in enumerate(other_shape):
            if r_shape is None:
                merged_shape += (ps,)
                continue

            rs = r_shape[i]
            if (
                # TODO FIXME: This is another instance of eager
                # canonicalization that we need to address.
                ps.owner is not None
                and isinstance(getattr(ps.owner, "op", None), Shape_i)
                and ps.owner.op.i == i
                and ps.owner.inputs[0] in (r, other_r)
            ):
                # If other_shape[i] is uninformative, use r_shape[i].
                # For now, we consider 2 cases of uninformative other_shape[i]:
                #  - Shape_i(i)(other_r);
                #  - Shape_i(i)(r).
                merged_shape += (rs,)
            elif isinstance(rs, Constant):
                # We always prefer constants
                merged_shape += (rs,)
            elif isinstance(ps, Constant):
                merged_shape += (ps,)
            elif ps == rs:
                # The shapes are equivalent.  We do not want to do the ancestor
                # check in those cases
                merged_shape += (rs,)
            elif (
                # TODO FIXME: This could be unnecessarily costly.
                rs
                in ancestors([ps])
            ):
                # Another case where we want to use r_shape[i] is when
                # other_shape[i] actually depends on r_shape[i]. In that case,
                # we do not want to substitute an expression with another that
                # is strictly more complex. Such a substitution could also lead
                # to cycles: if (in the future) r_shape[i] gets replaced by an
                # expression of other_shape[i], other_shape[i] may end up
                # depending on itself.
                merged_shape += (rs,)
            else:
                merged_shape += (ps,)

        assert all(
            (
                not isinstance(r.type, HasShape)
                or r.type.shape[i] != 1
                and other_r.type.shape[i] != 1
            )
            or self.lscalar_one.equals(merged_shape[i])
            or self.lscalar_one.equals(
                extract_constant(merged_shape[i], only_process_constants=True)
            )
            for i in range(r.type.ndim)
        )

        self.shape_of[r] = merged_shape
        for sv in merged_shape:
            self.shape_of_reverse_index.setdefault(sv, set()).add(r)

    def set_shape_i(self, r: Variable, i: int, s_i: Variable) -> None:
        """Replace element i of shape_of[r] by s_i"""

        prev_shape = self.shape_of[r]
        assert prev_shape is not None

        # prev_shape is a tuple, so we cannot change it inplace,
        # so we build another one.
        new_shape: Tuple[Variable, ...] = ()
        for j, s_j in enumerate(prev_shape):
            if j == i:
                new_shape += (self.to_symbolic_int(s_i),)
            else:
                new_shape += (s_j,)

        assert all(
            not isinstance(r.type, HasShape)
            or r.type.shape[idx] != 1
            or self.lscalar_one.equals(new_shape[idx])
            or self.lscalar_one.equals(extract_constant(new_shape[idx]))
            for idx in range(r.type.ndim)
        )

        self.shape_of[r] = new_shape

        for sv in new_shape:
            self.shape_of_reverse_index.setdefault(sv, set()).add(r)

    def init_r(self, r: Variable) -> None:
        """Register r's shape in the shape_of dictionary."""
        if r not in self.shape_of:
            self.set_shape(r, self.shape_tuple(r))

    def make_vector_shape(self, r: Variable) -> "TensorVariable":
        r_shape = self.shape_of[r]
        assert r_shape is not None
        return as_tensor_variable(r_shape, ndim=1, dtype="int64")

    def on_attach(self, fgraph):
        if hasattr(fgraph, "shape_feature"):
            raise AlreadyThere("This FunctionGraph already has a ShapeFeature")

        fgraph.shape_feature = self

        assert self.lscalar_one.type.dtype == "int64"

        self.shape_of: Dict[Variable, Optional[Tuple[Variable, ...]]] = {}
        self.scheduled: Dict["Apply", Variable] = {}
        self.shape_of_reverse_index: Dict[Variable, Set[Variable]] = {}

        for node in fgraph.toposort():
            self.on_import(fgraph, node, reason="on_attach")

    def on_detach(self, fgraph):
        self.shape_of.clear()
        self.scheduled.clear()
        self.shape_of_reverse_index.clear()
        del fgraph.shape_feature

    def on_import(self, fgraph, node, reason):
        if node.outputs[0] in self.shape_of:
            # this is a revert, not really an import
            for r in node.outputs + node.inputs:
                assert r in self.shape_of
            return

        for i, r in enumerate(node.inputs):
            # make sure we have shapes for the inputs
            self.init_r(r)

        o_shapes = self.get_node_infer_shape(fgraph, node)

        # this is packed information
        # an element of o_shapes is either None or a tuple
        #   elements of the tuple can be either strings, or ints
        if len(o_shapes) != len(node.outputs):
            raise Exception(
                (
                    f'The infer_shape method for the Op "{node.op}" returned a list '
                    f"with the wrong number of element: len(o_shapes) = {len(o_shapes)} "
                    f" != len(node.outputs) = {len(node.outputs)}"
                )
            )

        # Ensure shapes are in 'int64'. This is to make sure the assert
        # found in the `local_useless_subtensor` rewrite does not fail.
        for sh_idx, sh in enumerate(o_shapes):
            if sh is None:
                continue
            if not isinstance(sh, (list, tuple)):
                raise ValueError(
                    f"infer_shape of {node} didn't return a list of"
                    f" list. It returned '{o_shapes}'"
                )
            new_shape = []
            for i, d in enumerate(sh):
                # Note: we ignore any shape element that is not typed (i.e.,
                # does not have a 'dtype' attribute). This means there may
                # still remain int elements that are int32 on 32-bit platforms,
                # but this works with `local_useless_subtensor`, so for now we
                # keep it this way. See #266 for a better long-term fix.
                if getattr(d, "dtype", "int64") != "int64":
                    assert d.dtype in discrete_dtypes, (node, d.dtype)
                    assert str(d.dtype) != "uint64", node
                    new_shape += sh[len(new_shape) : i + 1]
                    if isinstance(d, Constant):
                        casted_d = constant(d.data, dtype="int64", ndim=0)
                    else:
                        casted_d = cast(d, "int64")
                    new_shape[i] = casted_d
            if new_shape:
                # We replace the shape with wrong dtype by the one with
                # 'int64'.
                new_shape += sh[len(new_shape) :]
                o_shapes[sh_idx] = tuple(new_shape)

        for r, s in zip(node.outputs, o_shapes):
            self.set_shape(r, s)

    def on_change_input(self, fgraph, node, i, r, new_r, reason):
        if new_r not in self.shape_of:
            # It happen that the fgraph didn't called on_import for some
            # new_r.  This happen when new_r don't have an
            # owner(i.e. it is a constant or an input of the graph)
            # update_shape suppose that r and new_r are in shape_of.
            self.init_r(new_r)

        # This tells us that r and new_r must have the same shape if
        # we didn't know that the shapes are related, now we do.
        self.update_shape(new_r, r)

        # change_input happens in two cases:
        # 1) we are trying to get rid of r, or
        # 2) we are putting things back after a failed transaction.

        # In case 1, if r has a shape_i client, we will want to
        # replace the shape_i of r with the shape of new_r.  Say that
        # r is *scheduled*.
        # At that point, node is no longer a client of r, but of new_r
        for shpnode, idx in fgraph.clients[r] + [(node, i)]:
            if isinstance(getattr(shpnode, "op", None), Shape_i):
                idx = shpnode.op.i
                repl = self.shape_of[new_r][idx]
                if repl.owner is shpnode:
                    # This mean the replacement shape object is
                    # exactly the same as the current shape object. So
                    # no need for replacement.
                    continue
                if (
                    repl.owner
                    and repl.owner.inputs[0] is shpnode.inputs[0]
                    and isinstance(repl.owner.op, Shape_i)
                    and repl.owner.op.i == shpnode.op.i
                ):
                    # The replacement is a shape_i of the same
                    # input. So no need to do this equivalent
                    # replacement.
                    continue

                if shpnode.outputs[0] in ancestors([repl]):
                    raise InconsistencyError(
                        "This substitution would insert a cycle in the graph:"
                        f"node: {node}, i: {i}, r: {r}, new_r: {new_r}"
                    )

                self.scheduled[shpnode] = new_r
        # In case 2, if r is a variable that we've scheduled for shape update,
        # then we should cancel it.
        unscheduled = [k for k, v in self.scheduled.items() if v == r]
        for k in unscheduled:
            del self.scheduled[k]

        # In either case, r could be in shape_of.values(), that is, r itself
        # is the shape of  something. In that case, we want to update
        # the value in shape_of, to keep it up-to-date.
        for v in self.shape_of_reverse_index.get(r, []):
            # The reverse index is only approximate. It is not updated on
            # deletion of variables, or on change_input so it might be the
            # case that there are a few extra `v`'s in it that no longer have
            # a shape of r or possibly have been deleted from shape_of
            # entirely. The important thing is that it permits to recall
            # all variables with r in their shape.
            for ii, svi in enumerate(self.shape_of.get(v, [])):
                if svi == r:
                    self.set_shape_i(v, ii, new_r)
        self.shape_of_reverse_index[r] = set()

    def same_shape(
        self,
        x: Variable,
        y: Variable,
        dim_x: Optional[int] = None,
        dim_y: Optional[int] = None,
    ) -> bool:
        """Return ``True`` if `x` and `y` have the same shape.

        Parameters
        ==========
        x
            The `Variable` for which its shape is to be compared with `y`'s shape.
        y
            The `Variable` for which its shape is to be compared with `x`'s shape.
        dim_x
            If non ``None``, compare only the dimension of `x` equal to
            `dim_x`.
        dim_y
            If non ``None``, compare only the dimension of `y` equal to
            `dim_y`.

        """
        sx = self.shape_of[x]
        sy = self.shape_of[y]

        if sx is None or sy is None:
            return False

        if dim_x is not None:
            sx = (sx[dim_x],)

        if dim_y is not None:
            sy = (sy[dim_y],)

        if len(sx) != len(sy):
            return False

        # Canonicalize the graphs so that comparisons are reasonable
        # TODO FIXME: This should *not* need to be performed manually here.
        # Instead, the shape information in `self.shape_of` should be operated
        # upon alongside all the other elements in a `FunctionGraph` (e.g. as
        # if `self.shape_of.values()` were additional outputs).
        shapes_fg = FunctionGraph(
            outputs=sx + sy,
            # features=[self],
            clone=True,
            # copy_inputs=False,
        )
        from aesara.graph.rewriting.utils import rewrite_graph

        canon_shapes_fg = type_cast(
            FunctionGraph,
            rewrite_graph(shapes_fg, custom_rewrite=topo_constant_folding),
        )
        canon_shapes = canon_shapes_fg.outputs

        sx_ = canon_shapes[: len(sx)]
        sy_ = canon_shapes[len(sx) :]

        for dx, dy in zip(sx_, sy_):
            if not equal_computations([dx], [dy]):
                return False

        return True

    def clone(self):
        return type(self)()


class ShapeOptimizer(GraphRewriter):
    """Rewriter that adds `ShapeFeature` as a feature."""

    def add_requirements(self, fgraph):
        fgraph.attach_feature(ShapeFeature())

    def apply(self, fgraph):
        pass


class UnShapeOptimizer(GraphRewriter):
    """Rewriter that removes `ShapeFeature` as a feature."""

    def apply(self, fgraph):
        for feature in fgraph._features:
            if isinstance(feature, ShapeFeature):
                fgraph.remove_feature(feature)


# Register it after merge1 optimization at 0. We don't want to track
# the shape of merged node.
aesara.compile.mode.optdb.register(  # type: ignore
    "ShapeOpt", ShapeOptimizer(), "fast_run", "fast_compile", position=0.1
)
# Not enabled by default for now. Some crossentropy opt use the
# shape_feature.  They are at step 2.01. uncanonicalize is at step
# 3. After it goes to 48.5 that move to the gpu. So 10 seems reasonable.
aesara.compile.mode.optdb.register("UnShapeOpt", UnShapeOptimizer(), position=10)  # type: ignore


def local_reshape_chain(op):
    @node_rewriter([op])
    def f(fgraph, node):
        """
        Reshape(Reshape(shape1),shape2) -> Reshape(shape2)

        """
        if not check_chain(node, op, op):
            return False

        # TODO: this can permit a failing program to run by eliminating
        #       the lower reshape
        rval = node.op(node.inputs[0].owner.inputs[0], node.inputs[1])

        # Copy over stacktrace from previous output node, as any error
        # in new computational graph would have been caused by last op
        # in the old computational graph.
        copy_stack_trace(node.outputs, rval)

        # It might happen that the desired output of this node has a
        # broadcastable pattern that does not match that of 'rval'. This is
        # when originally, we were able to figure out that one of the
        # dimensions of the reshape is one, but some other transformation
        # replaced the shape by one for which this cannot be guessed.
        # We should try to figure out why we lost the information about this
        # constant value... but in the meantime, better not apply this
        # rewrite.
        if rval.type.ndim == node.outputs[0].type.ndim and all(
            s1 == s1
            for s1, s2 in zip(rval.type.shape, node.outputs[0].type.shape)
            if s1 == 1 or s2 == 1
        ):
            return [rval]
        else:
            return False

    return f


register_canonicalize(local_reshape_chain(Reshape), name="local_reshape_chain")


@register_useless
@register_canonicalize
@register_stabilize
@node_rewriter([Reshape])
def local_useless_reshape(fgraph, node):
    """Remove two kinds of useless `Reshape`.

    - Remove `Reshape` when both the input and output have a single dimension.
    - Remove `Reshape` when reshaping to the shape of the input.

    """
    inp = node.inputs[0]
    output = node.outputs[0]
    output_shape = node.inputs[1]

    if inp.type.ndim != output.type.ndim:
        return False

    # Simple case: both input and output have a single dimension.
    # TODO FIXME XXX: This could hide errors if the user provides inconsistent
    # shapes.
    if (
        inp.type.ndim == 1
        and output.type.ndim == 1
        and all(
            s1 == s2
            for s1, s2 in zip(inp.type.shape, output.type.shape)
            if s1 == 1 or s2 == 1
        )
    ):
        return [inp]

    # Second case: all the shapes match the input shape
    # Match Reshape(x, x.shape)
    if output_shape.owner and isinstance(output_shape.owner.op, Shape):
        shape_input = output_shape.owner.inputs[0]
        if shape_input == inp:
            return [inp]

    # Match Reshape(x, [x.shape[0], ..., x.shape[-1]]), accounting for
    # broadcastable and constant dimensions
    if output_shape.owner and isinstance(output_shape.owner.op, MakeVector):
        output_shape_is = output_shape.owner.inputs

        shape_feature = getattr(fgraph, "shape_feature", None)

        nb_m1 = 0
        shape_match = [False] * inp.type.ndim
        for dim in range(inp.type.ndim):
            outshp_i = output_shape_is[dim]
            # Match Shape_i{dim}(input)
            if (
                outshp_i.owner
                and isinstance(outshp_i.owner.op, Shape_i)
                and outshp_i.owner.op.i == dim
                and outshp_i.owner.inputs[0] == inp
            ):
                shape_match[dim] = True
                continue

            # Match Shape(input)[dim]
            if (
                outshp_i.owner
                and isinstance(outshp_i.owner.op, Subtensor)
                and len(outshp_i.owner.inputs) == 2
                and extract_constant(outshp_i.owner.inputs[1]) == dim
            ):
                subtensor_inp = outshp_i.owner.inputs[0]
                if subtensor_inp.owner and isinstance(subtensor_inp.owner.op, Shape):
                    shape_input_i = subtensor_inp.owner.inputs[0]
                    if shape_input_i == inp:
                        shape_match[dim] = True
                        continue

            # Match 1 if input.type.shape[dim] == 1
            cst_outshp_i = extract_constant(outshp_i, only_process_constants=1)
            if inp.type.shape[dim] == 1 and cst_outshp_i == 1:
                shape_match[dim] = True
                continue

            # Match -1
            if cst_outshp_i == -1:
                shape_match[dim] = True
                nb_m1 += 1
                continue

            # Match shape_of[input][dim] or its constant equivalent
            if shape_feature:
                inpshp_i = shape_feature.get_shape(fgraph, inp, dim)
                if inpshp_i == outshp_i or (
                    extract_constant(inpshp_i, only_process_constants=1)
                    == extract_constant(outshp_i, only_process_constants=1)
                ):
                    shape_match[dim] = True
                    continue

        if all(shape_match) and nb_m1 <= 1:
            return [inp]

        # TODO later: if all the shapes except one match, we may want to
        # consider it useless as well, like we do in the 1-dim case.
        return False


@register_canonicalize
@node_rewriter([Reshape])
def local_reshape_to_dimshuffle(fgraph, node):
    r"""Replace broadcastable dimensions in `Reshape` nodes with `DimShuffle`\s.

    The goal is to avoid using `Reshape` to add or remove broadcastable
    dimensions, and to use `DimShuffle` instead, since `DimShuffle`\s can
    cancel out and/or be removed later on.

    For example:
        - reshape(x, (1, n)) -> DimShuffle{x,0}(Reshape(x, (n,))
        - reshape(x, (1, m, 1, n, 1, 1))
          -> DimShuffle{x,0,x,1,x,x}(Reshape(x, (m, n)))
    """
    op = node.op
    inp = node.inputs[0]
    output = node.outputs[0]
    output_shape = node.inputs[1]

    dimshuffle_new_order = []
    new_output_shape = []
    index = 0  # index over the output of the new reshape
    for i in range(output.ndim):
        # Since output_shape is a symbolic vector, we trust extract_constant
        # to go through however it is formed to see if its i-th element is 1.
        # We need only_process_constants=False for that.
        dim = extract_constant(
            output_shape[i], only_process_constants=False, elemwise=False
        )
        if dim == 1:
            dimshuffle_new_order.append("x")
        else:
            dimshuffle_new_order.append(index)
            new_output_shape.append(dim)
            index = index + 1

    if index != output.type.ndim:
        inner = op.__class__(len(new_output_shape))(inp, new_output_shape)
        copy_stack_trace(output, inner)
        new_node = [
            DimShuffle(tuple(s == 1 for s in inner.type.shape), dimshuffle_new_order)(
                inner
            )
        ]
        copy_stack_trace(output, new_node)
        return new_node


@register_canonicalize
@register_stabilize
@node_rewriter([Reshape])
def local_reshape_lift(fgraph, node):
    """
        Reshape(UnaryElemwise(x)) -> UnaryElemwise(Reshape(x))

    Notes
    -----
    This rewrite is needed by `log1msigm_to_softplus` in order to get applied
    when there is a reshape.

    """
    if (
        isinstance(node.op, Reshape)
        and node.inputs[0].owner
        and isinstance(node.inputs[0].owner.op, Elemwise)
        and len(node.inputs[0].owner.inputs) == 1
    ):
        r = node.op(node.inputs[0].owner.inputs[0], node.inputs[1])
        # Copy stacktrace from previous Reshape op, as an error in new
        # Reshape op could only have been caused by old one.
        copy_stack_trace(node.outputs, r)

        e = node.inputs[0].owner.op(r)
        # Copy stacktrace from both previous Reshape and UnaryElemwise op
        # because an error in new cg could have been caused by either ops.
        copy_stack_trace(node.outputs + node.inputs, e)
        return [e]


@register_useless
@register_canonicalize
@node_rewriter([SpecifyShape])
def local_merge_consecutive_specify_shape(fgraph, node):
    """Replace ``specify_shape(specify_shape(x, s1), s2)`` with ``specify_shape(x, s3)``,
    where s3 is the union of specified dimensions in s1 and s2, with preference given to s2.
    """

    if not isinstance(node.op, SpecifyShape):
        return False

    obj = node.inputs[0]
    if not (obj.owner and isinstance(obj.owner.op, SpecifyShape)):
        return False

    inner_obj, *shape = obj.owner.inputs
    for dim, sh in enumerate(node.inputs[1:]):
        if not NoneConst.equals(sh):
            shape[dim] = sh

    # TODO: We could make sure that the overlapping shapes of the two `SpecifyShape`s are
    # the same.

    return [specify_shape(inner_obj, shape)]


@register_useless
@register_canonicalize
@node_rewriter([Shape])
def local_Shape_of_SpecifyShape(fgraph, node):
    """Replace ``specify_shape(x, s).shape`` with ``s``."""

    if not isinstance(node.op, Shape):
        return False

    specified_shape = node.inputs[0]

    if not isinstance(getattr(specified_shape.owner, "op", None), SpecifyShape):
        return False

    x, *shape = specified_shape.owner.inputs

    # Replace `NoneConst` by `shape_i`
    for i, sh in enumerate(shape):
        if NoneConst.equals(sh):
            shape[i] = shape_i(x, i, fgraph)

    return [stack(shape).astype(np.int64)]


@register_useless
@register_canonicalize
@node_rewriter([Shape_i])
def local_Shape_i_ground(fgraph, node):
    """Replace ``shape_i(x, i)`` with ``s`` when ``x.type.shape[i] == s``."""

    if not isinstance(node.op, Shape_i):
        return False

    shape_arg = node.inputs[0]

    if not isinstance(shape_arg.type, TensorType):
        return False

    s_val = shape_arg.type.shape[node.op.i]
    if s_val is not None:
        return [as_tensor_variable(s_val, dtype=np.int64)]


@register_specialize
@register_canonicalize
@node_rewriter([Shape])
def local_shape_to_shape_i(fgraph, node):
    if isinstance(node.op, Shape):
        if not hasattr(fgraph, "shape_feature"):
            return
        shape_feature = fgraph.shape_feature
        ret = shape_feature.make_vector_shape(node.inputs[0])

        # We need to copy over stack trace from input to output
        copy_stack_trace(node.outputs[0], ret)
        return [ret]


@register_specialize
@register_canonicalize
@node_rewriter([Shape_i])
def local_track_shape_i(fgraph, node):
    if not isinstance(node.op, Shape_i):
        return False

    try:
        shape_feature = fgraph.shape_feature
    except AttributeError:
        return False

    if node not in shape_feature.scheduled:
        return False

    # Don't unschedule node as it could be reinserted in the
    # fgraph as we don't change it in the shapefeature internal
    # structure.
    replacement = shape_feature.scheduled[node]
    return [shape_feature.shape_of[replacement][node.op.i]]


@register_canonicalize
@node_rewriter([Reshape])
def local_useless_dimshuffle_in_reshape(fgraph, node):
    """
    Removes useless DimShuffle operation inside Reshape:

      reshape(vector.dimshuffle('x', 0), shp) => reshape(vector, shp)
      reshape(matrix.dimshuffle('x', 0, 'x', 1), shp) => reshape(matrix, shp)
      reshape(row.dimshuffle(1, 'x'), shp) => reshape(row, shp)
      reshape(col.dimshuffle(0), shp) => reshape(col, shp)

    """
    op = node.op
    if not isinstance(op, Reshape):
        return False
    if not (
        node.inputs[0].owner is not None
        and isinstance(node.inputs[0].owner.op, DimShuffle)
    ):
        return False

    new_order = node.inputs[0].owner.op.new_order
    inp = node.inputs[0].owner.inputs[0]
    new_order_of_nonbroadcast = []
    for i, s in zip(new_order, node.inputs[0].type.shape):
        if s != 1:
            new_order_of_nonbroadcast.append(i)
    no_change_in_order = all(
        new_order_of_nonbroadcast[i] <= new_order_of_nonbroadcast[i + 1]
        for i in range(len(new_order_of_nonbroadcast) - 1)
    )
    if no_change_in_order:
        shape = node.inputs[1]
        ret = op.__class__(node.outputs[0].ndim)(inp, shape)
        copy_stack_trace(node.outputs[0], ret)
        return [ret]


@register_useless
@register_canonicalize
@register_specialize
@node_rewriter([Unbroadcast])
def local_useless_unbroadcast(fgraph, node):
    """Remove `Unbroadcast` if it does not actually change the broadcasting pattern.

    TODO: Implement equivalent rewrite for SpecifyShape
    """
    if isinstance(node.op, Unbroadcast):
        x = node.inputs[0]
        if x.type.ndim == node.outputs[0].type.ndim and all(
            s1 == s2
            for s1, s2 in zip(x.type.shape, node.outputs[0].type.shape)
            if s1 == 1 or s2 == 1
        ):
            # No broadcastable flag was modified
            # No need to copy over stack trace,
            # because x should already have a stack trace.
            return [x]
        else:
            # Keep the flags that modify something
            new_axes = tuple(ax for ax in node.op.axes if x.type.shape[ax] == 1)
            if new_axes == node.op.axes:
                # All flags are useful
                return None
            else:
                r = unbroadcast(x, *new_axes)
                # Copy over stacktrace from previous output
                copy_stack_trace(node.outputs, r)
                return [r]


@register_canonicalize
@register_specialize
@node_rewriter([Unbroadcast])
def local_unbroadcast_lift(fgraph, node):
    """
    Lifts `Unbroadcast` through unary Elemwise operations,
    and merges consecutive `Unbroadcast`s.

    Unbroadcast(Elemwise(x)) => Elemwise(Unbroadcast(x))
    Unbroadcast(Unbroadcast(x)) => Unbroadcast(x)

    TODO: Implement equivalent Elemwise lift for SpecifyShape
    """
    op = node.op
    if not isinstance(op, Unbroadcast):
        return False

    inp = node.inputs[0]
    inode = inp.owner
    if inode and isinstance(inode.op, Elemwise) and len(inode.inputs) == 1:
        if len(fgraph.clients.get(inp, ())) == 1:
            unbroadcasted = unbroadcast(inode.inputs[0], *op.axes)
            copy_stack_trace(node.outputs, unbroadcasted)

            rval = inode.op.make_node(unbroadcasted).outputs

            # Copy over stacktrace from previous output (after unbroadcasting)
            # and input (after elemwise operation) to new output, because an
            # error in the new graph could have been caused by either of the
            # two ops.
            copy_stack_trace(node.outputs + node.inputs, rval)
            return rval

    if inode and isinstance(inode.op, Unbroadcast):
        # Merge axis of each unbroadcast
        axis = tuple(set(inode.op.axes).union(set(op.axes)))
        iinput = inode.inputs[0]
        rval = [unbroadcast(iinput, *axis)]
        # Copy over stacktrace from previous output (after second unbroadcasting)
        # and from previous input (after first unbroadcasting) because an error in
        # the new graph could have been caused by either of the two Unbroadcast ops.
        copy_stack_trace(node.outputs + node.inputs, rval)
        return rval

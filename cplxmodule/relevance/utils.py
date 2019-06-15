import scipy
import scipy.special

import torch
import torch.sparse

import torch.nn.functional as F

from ..cplx import Cplx


class ExpiFunction(torch.autograd.Function):
    r"""Pythonic differentiable port of scipy's Exponential Integral Ei.

    $$
        Ei
            \colon \mathbb{R} \to \mathbb{R} \cup \{\pm \infty\}
            \colon x \mapsto \int_{-\infty}^x \tfrac{e^t}{t} dt
        \,. $$

    Notes
    -----
    This may potentially introduce a memory transfer and compute bottleneck
    during the forward pass due to CPU-GPU device switch. Backward pass does
    not suffer from this issue and is compute on-device.
    """
    @staticmethod
    def forward(ctx, x):
        ctx.save_for_backward(x)

        x_cpu = x.data.cpu().numpy()
        output = scipy.special.expi(x_cpu, dtype=x_cpu.dtype)
        return torch.from_numpy(output).to(x.device)

    @staticmethod
    def backward(ctx, grad_output):
        x = ctx.saved_tensors[-1]
        return grad_output * torch.exp(x) / x


torch_expi = ExpiFunction.apply


def kldiv_approx(log_alpha, coef, reduction):
    r"""Sofplus-sigmoid approximation.
    $$
        \alpha \mapsto
            k_1 \sigma(k_2 + k_3 \log \alpha) + C
                - k_4 \log (1 + e^{-\log \alpha})
        \,, $$
    for $C = - k_1$.
    """
    if reduction is not None and reduction not in ("mean", "sum"):
        raise ValueError("""`reduction` must be either `None`, "sum" """
                         """or "mean".""")

    k1, k2, k3, k4 = coef
    C = -k1

    # $x \mapsto \log(1 + e^x)$ is softplus and needs different
    #  compute paths depending on the sign of $x$:
    #  $$ x\mapsto \log(1+e^{-\lvert x\rvert}) + \max{\{x, 0\}} \,. $$
    sigmoid = torch.sigmoid(k2 + k3 * log_alpha)
    softplus = - k4 * F.softplus(- log_alpha)
    kl_div = k1 * sigmoid + softplus + C

    if reduction == "mean":
        return kl_div.mean()

    elif reduction == "sum":
        return kl_div.sum()

    return kl_div


def torch_sparse_tensor(indices, data, shape):
    if data.dtype is torch.float:
        return torch.sparse.FloatTensor(indices, data, shape)

    elif data.dtype is torch.double:
        return torch.sparse.DoubleTensor(indices, data, shape)

    raise TypeError(f"""Unsupported dtype `{data.dtype}`""")


def torch_sparse_linear(input, weight, bias=None):
    *head, n_features = input.shape
    x = input.reshape(-1, n_features)

    out = torch.sparse.mm(weight, x.t()).t()
    out = out.reshape(*head, weight.shape[0])

    if bias is not None:
        out += bias

    return out


def torch_sparse_cplx_linear(input, weight, bias=None):
    #  W z + c = (U + i V) (u + i v) + \Re c + i \Im c
    #          = (U u + \Re c - V v) + i (V u + \Im c + U v)
    real = torch_sparse_linear(input.real, weight.real, None) \
        - torch_sparse_linear(input.imag, weight.imag, None)
    imag = torch_sparse_linear(input.real, weight.imag, None) \
        + torch_sparse_linear(input.imag, weight.real, None)

    output = Cplx(real, imag)
    if isinstance(bias, Cplx):
        output += bias

    return output


def parameter_to_buffer(module, name):
    # par could be a solo parameter or a container (essentially a submodule)
    par = getattr(module, name)
    if isinstance(par, (torch.nn.ParameterDict, torch.nn.ParameterList)):
        # parameter containers no not use buffers and aren't expected to.
        #  So we hide parameters there. This precludes acces via __getitem__
        #  though. Not via __getattr__

        # create a copy of the container's master parameter dict's keys and mutate
        for name in list(par._parameters):
            # By design of Parameter containers this never recurses deeper
            parameter_to_buffer(par, name)
        return

    # a solo parameter
    if par is not None and not isinstance(par, torch.nn.Parameter):
        raise KeyError(f"parameter '{name}' is not a tensor.")

    # remove the parameter and mutate into a grad-detached buffer
    delattr(module, name)
    par = par.detach() if par is not None else None
    module.register_buffer(name, par)


def buffer_to_parameter(module, name):
    # a buffer here can be a buffer or a former mutated parameter container
    buf = getattr(module, name)
    if isinstance(buf, (torch.nn.ParameterDict, torch.nn.ParameterList)):
        # create a copy of the container's master buffer dict's keys and restore
        for name in list(buf._buffers):
            # By design of Parameter containers this never goes deeper
            #  than this call
            buffer_to_parameter(buf, name)
        return

    if buf is not None and not isinstance(buf, torch.Tensor):
        raise KeyError(f"buffer '{name}' is not a tensor.")

    # remove the buffer and mutate back into a proper parameter
    delattr(module, name)
    buf = torch.nn.Parameter(buf) if buf is not None else None
    module.register_parameter(name, buf)

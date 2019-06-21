import torch
import torch.nn

import torch.nn.functional as F

from .utils import kldiv_approx
from .base import BaseARD


def real_nkldiv_apprx(log_alpha, reduction="mean"):
    r"""
    Approximation of the negative Kl divergence from arxiv:1701.05369.
    $$
        - KL(\mathcal{N}(w\mid \theta, \alpha \theta^2) \|
                \tfrac1{\lvert w \rvert})
            = \tfrac12 \log \alpha
              - \mathbb{E}_{\xi \sim \mathcal{N}(1, \alpha)}
                \log{\lvert \xi \rvert} + C
        \,. $$
    """
    coef = 0.63576, 1.87320, 1.48695, 0.5
    return kldiv_approx(log_alpha, coef, reduction)


class LinearARD(torch.nn.Linear, BaseARD):
    __ard_ignore__ = ("log_sigma2",)

    def __init__(self, in_features, out_features, bias=True, reduction="mean"):
        super().__init__(in_features, out_features, bias=bias)
        self.reduction = reduction

        self.log_sigma2 = torch.nn.Parameter(torch.Tensor(*self.weight.shape))
        self.reset_variational_parameters()

    def reset_variational_parameters(self):
        # initially everything is relevant
        self.log_sigma2.data.uniform_(-10, -10)

    @property
    def log_alpha(self):
        r"""Get $\log \alpha$ from $(\theta, \sigma^2)$ parameterization."""
        return self.log_sigma2 - 2 * torch.log(abs(self.weight) + 1e-12)

    @property
    def penalty(self):
        r"""Compute the variational penalty term."""
        # neg KL divergence must be maximized, hence the -ve sign.
        return - real_nkldiv_apprx(self.log_alpha, reduction=self.reduction)

    def relevance(self, threshold):
        r"""Get the relevance mask based on the threshold."""
        with torch.no_grad():
            return torch.le(self.log_alpha, threshold).to(self.log_alpha)

    def _sparsity(self, threshold, hard=None):
        n_relevant = float(self.relevance(threshold).sum().item())
        return [(id(self.weight), self.weight.numel() - n_relevant)]

    def forward(self, input):
        mu = super().forward(input)
        # mu = F.linear(input, self.weight, self.bias)
        if not self.training:
            return mu
        # end if

        s2 = F.linear(input * input, torch.exp(self.log_sigma2), None)
        return mu + torch.randn_like(s2) * torch.sqrt(s2 + 1e-20)

"""
Loss functions
"""

import os
import sys
import functools
from typing import *

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter

from babel_my.models import layers


class BCELoss(nn.BCELoss):
	"""Custom BCE loss that can correctly ignore the encoded latent space output"""

	def forward(self, x, target):
		input = x[0]
		return F.binary_cross_entropy(
			input, target, weight=self.weight, reduction=self.reduction
		)


class L1Loss(nn.L1Loss):
	"""Custom L1 loss that ignores all but first input"""

	def forward(self, x, target) -> torch.Tensor:
		return F.l1_loss(x[0], target, reduction=self.reduction)


class ClassWeightedBCELoss(nn.BCELoss):
	"""BCE that has different weight for 1/0 class"""

	def __init__(self, class0_weight: float, class1_weight: float, **kwargs):
		super(ClassWeightedBCELoss, self).__init__(**kwargs)
		self.w0 = torch.tensor(class0_weight)
		self.w1 = torch.tensor(class1_weight)

	def forward(self, preds, target):
		# This is batch size x num_features
		bce = F.binary_cross_entropy(
			preds, target, weight=self.weight, reduction="none"
		)
		weights = torch.where(
			target > 0, self.w1.to(preds.device), self.w0.to(preds.device)
		)
		retval = weights * bce
		assert retval.shape == preds.shape
		return torch.mean(retval)


class LogProbLoss(nn.Module):
	"""
	Log probability loss (originally written for RealNVP). Negates output (because log)

	The prior needs to support a .log_prob(x) method
	"""

	def __init__(self, prior):
		super(LogProbLoss, self).__init__()
		self.prior = prior

	def forward(self, x, _target=None):
		z, logp = x[:2]
		p = self.prior.log_prob(z)
		if len(p.shape) == 2:
			p = torch.mean(p, dim=1)
		per_ex = p + logp
		# assert len(per_ex) == z.shape[0]
		retval = -torch.mean(per_ex)
		if retval != retval:  # Detect NaN
			raise ValueError(f"Got NaN for loss with input z and logp: {z} {logp}")
		# if retval < 0:
		#     raise ValueError(f"Got negative loss with input z and logp: {z} {logp}")
		return retval


class DistanceProbLoss(nn.Module):
	"""
	Analog of above log prob loss, but using distances

	May be useful for aligning latent spaces
	"""

	def __init__(self, weight: float = 5.0, norm: int = 1):
		super(DistanceProbLoss, self).__init__()
		assert weight > 0
		self.weight = weight
		self.norm = norm

	def forward(self, x, target_z):
		z, logp = x[:2]
		d = F.pairwise_distance(
			z,
			target_z,
			p=self.norm,
			eps=1e-6,
			keepdim=False,  # Default value
		)
		if len(d.shape) == 2:
			d = torch.mean(d, dim=1)  # Drop 1 dimension
		per_ex = self.weight * d - logp
		retval = torch.mean(per_ex)
		if retval != retval:
			raise ValueError("NaN")
		return retval
		return torch.mean(d)


class MSELoss(nn.MSELoss):
	"""MSE loss"""

	def forward(self, x: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
		return F.mse_loss(x[0], target, reduction=self.reduction)


class MSELogLoss(nn.modules.loss._Loss):
	"""
	MSE loss after applying log2

	Based on:
	https://pytorch.org/docs/stable/_modules/torch/nn/modules/loss.html#MSELoss
	"""

	__constants__ = ["reduction"]

	def __init__(self, size_average=None, reduce=None, reduction="mean"):
		super(MSELogLoss, self).__init__(size_average, reduce, reduction)

	def forward(self, input, target):
		input_log = torch.log1p(input)
		target_log = torch.log1p(target)
		return F.mse_loss(input_log, target_log, reduction=self.reduction)


class MyNegativeBinomialLoss(nn.Module):
	"""
	Re-derived negative binomial loss.
	"""

	def __init__(self):
		super(MyNegativeBinomialLoss, self).__init__()

	def forward(self, preds, target):
		preds, theta = preds[:2]
		# Compare to:
		# reconst_loss = vae.get_reconstruction_loss(sample_batch, px_rate, px_r, px_dropout)
		l = -scvi_log_nb_positive(target, preds, theta)
		return l


class NegativeBinomialLoss(nn.Module):
	"""
	Negative binomial loss. Preds should be a tuple of (mean, dispersion)
	"""

	def __init__(
		self,
		scale_factor: float = 1.0,
		eps: float = 1e-10,
		l1_lambda: float = 0.0,
		mean: bool = True,
	):
		super(NegativeBinomialLoss, self).__init__()
		self.loss = negative_binom_loss(
			scale_factor=scale_factor,
			eps=eps,
			mean=mean,
			debug=True,
		)
		self.l1_lambda = l1_lambda

	def forward(self, outp, batch):
		preds, theta = outp[:2]
		target = batch[0]

		l = self.loss(
			preds=preds,
			theta=theta,
			truth=target,
		)

		if self.l1_lambda:
			encoded = outp[-1]
			l += self.l1_lambda * torch.abs(encoded).sum()

		return l


class ZeroInflatedNegativeBinomialLoss(nn.Module):
	"""
	ZINB loss. Preds should be a tuple of (mean, dispersion, dropout)

	General notes:
	total variation seems to do poorly (at least for atacseq)
	"""

	def __init__(
		self,
		ridge_lambda: float = 0.0,
		tv_lambda: float = 0.0,
		l1_lambda: float = 0.0,
		eps: float = 1e-10,
		scale_factor: float = 1.0,
		debug: bool = True,
	):
		super(ZeroInflatedNegativeBinomialLoss, self).__init__()
		self.loss = zero_inflated_negative_binom_loss(
			ridge_lambda=ridge_lambda,
			tv_lambda=tv_lambda,
			eps=eps,
			scale_factor=scale_factor,
			debug=debug,
		)
		self.l1_lambda = l1_lambda

	def forward(self, outp, target):
		preds, theta, pi = outp[:3]
		l = self.loss(
			preds=preds,
			theta_disp=theta,
			pi_dropout=pi,
			truth=target,
		)
		encoded = outp[-1]
		l += self.l1_lambda * torch.abs(encoded).sum()
		return l


class MyZeroInflatedNegativeBinomialLoss(nn.Module):
	"""
	ZINB loss, based on scvi
	"""

	def forward(self, preds, target):
		preds, theta, pi = preds[:3]
		l = -scvi_log_zinb_positive(target, preds, theta, pi)
		return l


class PairedLoss(nn.Module):
	"""
	Paired loss function. Automatically unpacks and encourages the encoded representation to be similar
	using a given distance function. link_strength parameter controls how strongly we encourage this
	loss2_weight controls how strongly we weight the second loss, relative to the first
	A value of 1.0 indicates that they receive equal weight, and a value larger indicates
	that the second loss receives greater weight.

	link_func should be a callable that takes in the two encoded representations and outputs a metric
	where a larger value indicates greater divergence
	"""

	def __init__(
		self,
		loss1=NegativeBinomialLoss,
		loss2=ZeroInflatedNegativeBinomialLoss,
		link_func=lambda x, y: (x - y).abs().mean(),
		link_strength=1e-3,
	):
		super(PairedLoss, self).__init__()
		self.loss1 = loss1()
		self.loss2 = loss2()
		self.link = link_strength
		self.link_f = link_func

		self.warmup = layers.SigmoidWarmup(
			midpoint=1000,
			maximum=link_strength,
		)

	def forward(self, preds, target):
		"""Unpack and feed to each loss, averaging at end"""
		preds1, preds2 = preds
		target1, target2 = target

		loss1 = self.loss1(preds1, target1)
		loss2 = self.loss2(preds2, target2)
		retval = loss1 + loss2

		# Align the encoded representation assuming the last output is encoded representation
		encoded1 = preds1[-1]
		encoded2 = preds2[-1]
		if self.link > 0:
			l = next(self.warmup)
			if l > 1e-6:
				d = self.link_f(encoded1, encoded2).mean()
				retval += l * d

		return retval


class PairedLossInvertible(nn.Module):
	"""
	Paired loss function with additional invertible (RealNVP) layer loss
	Loss 1 is for the first autoencoder
	Loss 2 is for the second autoencoder
	Loss 3 is for the invertible network at bottleneck
	"""

	def __init__(
		self,
		loss1=NegativeBinomialLoss,
		loss2=ZeroInflatedNegativeBinomialLoss,
		loss3=DistanceProbLoss,
		link_func=lambda x, y: (x - y).abs().mean(),
		link_strength=1e-3,
		inv_strength=1.0,
	):
		super(PairedLossInvertible, self).__init__()
		self.loss1 = loss1()
		self.loss2 = loss2()
		self.loss3 = loss3()
		self.link = link_strength
		self.link_f = link_func

		# self.link_warmup = layers.SigmoidWarmup(
		#     midpoint=1000,
		#     maximum=link_strength,
		# )
		self.link_warmup = layers.DelayedLinearWarmup(
			delay=1000,
			inc=5e-3,
			t_max=link_strength,
		)

		self.inv_warmup = layers.DelayedLinearWarmup(
			delay=2000,
			inc=5e-3,
			t_max=inv_strength,
		)

	def forward(self, preds, target):
		"""Unpack and feed to each loss"""
		# Both enc1_pred and enc2_pred are tuples of 2 values
		preds1, preds2, (enc1_pred, enc2_pred) = preds
		target1, target2 = target

		loss1 = self.loss1(preds1, target1)
		loss2 = self.loss2(preds2, target2)
		retval = loss1 + loss2

		# Align the encoded representations
		encoded1 = preds1[-1]
		encoded2 = preds2[-1]
		if self.link > 0:
			l = next(self.link_warmup)
			if l > 1e-6:
				d = self.link_f(encoded1, encoded2).mean()
				retval += l * d

		# Add a term for invertible network
		inv_loss1 = self.loss3(enc1_pred, enc2_pred[0])
		inv_loss2 = self.loss3(enc2_pred, enc1_pred[0])
		retval += next(self.inv_warmup) * (inv_loss1 + inv_loss2)

		return retval


def get_QuadLoss(loss_weight):
	return QuadLoss(
			loss2=BCELoss,
			loss2_weight=loss_weight,
			record_history=False
	)


def getNBLoss(*args):
	return NegativeBinomialLoss()


class QuadLoss(PairedLoss):
	"""
	Paired loss, but for the spliced autoencoder with 4 outputs
	"""

	def __init__(
		self,
		loss1=NegativeBinomialLoss,
		loss2=BCELoss,
		loss2_weight: float = 3.0,
		cross_weight: float = 1.0,
		cross_warmup_delay: int = 0,
		link_strength: float = 0.0,
		link_func: Callable = lambda x, y: (x - y).abs().mean(),
		link_warmup_delay: int = 0,
		record_history: bool = False,
	):
		super(QuadLoss, self).__init__()
		self.loss1 = loss1()
		self.loss2 = loss2()
		self.loss2_weight = loss2_weight
		self.history = []  # Eventually contains list of tuples per call
		self.record_history = record_history

		if link_warmup_delay:
			self.warmup = layers.SigmoidWarmup(
				midpoint=link_warmup_delay,
				maximum=link_strength,
			)
			# self.warmup = layers.DelayedLinearWarmup(
			#     delay=warmup_delay,
			#     t_max=link_strength,
			#     inc=1e-3,
			# )
		else:
			self.warmup = layers.NullWarmup(t_max=link_strength)
		if cross_warmup_delay:
			self.cross_warmup = layers.SigmoidWarmup(
				midpoint=cross_warmup_delay,
				maximum=cross_weight,
			)
		else:
			self.cross_warmup = layers.NullWarmup(t_max=cross_weight)

		self.link_strength = link_strength
		self.link_func = link_func

	def get_component_losses(self, preds, target):
		"""
		Return the four losses that go into the overall loss, without scaling
		"""
		preds11, preds12, preds21, preds22 = preds
		if not isinstance(target, (list, tuple)):
			# Try to unpack into the correct parts
			target = torch.split(
				target, [preds11[0].shape[-1], preds22[0].shape[-1]], dim=-1
			)
		target1, target2 = target  # Both are torch tensors

		loss11 = self.loss1(preds11, target1)
		loss21 = self.loss1(preds21, target1)
		loss12 = self.loss2(preds12, target2)
		loss22 = self.loss2(preds22, target2)

		return loss11, loss21, loss12, loss22

	def forward(self, preds, target):
		loss11, loss21, loss12, loss22 = self.get_component_losses(preds, target)
		if self.record_history:
			detensor = lambda x: x.detach().cpu().numpy().item()
			self.history.append([detensor(l) for l in (loss11, loss21, loss12, loss22)])

		loss = loss11 + self.loss2_weight * loss22
		loss += next(self.cross_warmup) * (loss21 + self.loss2_weight * loss12)

		if self.link_strength > 0:
			l = next(self.warmup)
			if l > 1e-6:  # If too small we disregard
				preds11, preds12, preds21, preds22 = preds
				encoded1 = preds11[-1]  # Could be preds12
				encoded2 = preds22[-1]  # Could be preds21
				d = self.link_func(encoded1, encoded2)
				loss += self.link_strength * d
		return loss


def scvi_log_nb_positive(x, mu, theta, eps=1e-8):
	"""
	Taken from scVI log_likelihood.py - scVI invocation is:
	reconst_loss = -log_nb_positive(x, px_rate, px_r).sum(dim=-1)
	scVI decoder outputs px_scale, px_r, px_rate, px_dropout
	px_scale is subject to Softmax
	px_r is just a Linear layer
	px_rate = torch.exp(library) * px_scale

	mu = mean of NB
	theta = indverse dispersion parameter

	Here, x appears to correspond to y_true in the below negative_binom_loss (aka the observed counts)
	"""
	# if theta.ndimension() == 1:
	#     theta = theta.view(
	#         1, theta.size(0)
	#     )  # In this case, we reshape theta for broadcasting

	log_theta_mu_eps = torch.log(theta + mu + eps)
	res = (
		theta * (torch.log(theta + eps) - log_theta_mu_eps)
		+ x * (torch.log(mu + eps) - log_theta_mu_eps)
		+ torch.lgamma(x + theta)
		- torch.lgamma(theta)  # Present (in negative) for DCA
		- torch.lgamma(x + 1)
	)

	return res.mean()


def scvi_log_zinb_positive(x, mu, theta, pi, eps=1e-8):
	"""
	https://github.com/YosefLab/scVI/blob/6c9f43e3332e728831b174c1c1f0c9127b77cba0/scvi/models/log_likelihood.py#L206
	"""
	# theta is the dispersion rate. If .ndimension() == 1, it is shared for all cells (regardless of batch or labels)
	if theta.ndimension() == 1:
		theta = theta.view(
			1, theta.size(0)
		)  # In this case, we reshape theta for broadcasting

	softplus_pi = F.softplus(-pi)
	log_theta_eps = torch.log(theta + eps)
	log_theta_mu_eps = torch.log(theta + mu + eps)
	pi_theta_log = -pi + theta * (log_theta_eps - log_theta_mu_eps)

	case_zero = F.softplus(pi_theta_log) - softplus_pi
	mul_case_zero = torch.mul((x < eps).type(torch.float32), case_zero)

	case_non_zero = (
		-softplus_pi
		+ pi_theta_log
		+ x * (torch.log(mu + eps) - log_theta_mu_eps)  # Found above
		+ torch.lgamma(x + theta)  # Found above
		- torch.lgamma(theta)  # Found above
		- torch.lgamma(x + 1)  # Found above
	)
	mul_case_non_zero = torch.mul((x > eps).type(torch.float32), case_non_zero)

	res = mul_case_zero + mul_case_non_zero

	return res.mean()


def negative_binom_loss(
	scale_factor: float = 1.0,
	eps: float = 1e-10,
	mean: bool = True,
	debug: bool = False,
	tb: SummaryWriter = None,
) -> Callable:
	"""
	Return a function that calculates the binomial loss
	https://github.com/theislab/dca/blob/master/dca/loss.py

	combination of the Poisson distribution and a gamma distribution is a negative binomial distribution
	"""

	def loss(preds, theta, truth, tb_step: int = None):
		"""Calculates negative binomial loss as defined in the NB class in link above"""
		y_true = truth
		y_pred = preds * scale_factor

		if debug:  # Sanity check before loss calculation
			assert not torch.isnan(y_pred).any(), y_pred
			assert not torch.isinf(y_pred).any(), y_pred
			assert not (y_pred < 0).any()  # should be non-negative
			assert not (theta < 0).any()

		# Clip theta values
		theta = torch.clamp(theta, max=1e6)

		t1 = (
			torch.lgamma(theta + eps)
			+ torch.lgamma(y_true + 1.0)
			- torch.lgamma(y_true + theta + eps)
		)
		t2 = (theta + y_true) * torch.log1p(y_pred / (theta + eps)) + (
			y_true * (torch.log(theta + eps) - torch.log(y_pred + eps))
		)
		if debug:  # Sanity check after calculating loss
			assert not torch.isnan(t1).any(), t1
			assert not torch.isinf(t1).any(), (t1, torch.sum(torch.isinf(t1)))
			assert not torch.isnan(t2).any(), t2
			assert not torch.isinf(t2).any(), t2

		retval = t1 + t2
		if debug:
			assert not torch.isnan(retval).any(), retval
			assert not torch.isinf(retval).any(), retval

		if tb is not None and tb_step is not None:
			tb.add_histogram("nb/t1", t1, global_step=tb_step)
			tb.add_histogram("nb/t2", t2, global_step=tb_step)

		return torch.mean(retval) if mean else retval

	return loss


def zero_inflated_negative_binom_loss(
	ridge_lambda: float = 0.0,
	tv_lambda: float = 0.0,
	eps: float = 1e-10,
	scale_factor: float = 1.0,
	debug: bool = False,
	tb: SummaryWriter = None,
):
	"""
	Return a function that calculates ZINB loss
	https://github.com/theislab/dca/blob/master/dca/loss.py
	"""
	nb_loss_func = negative_binom_loss(
		mean=False, eps=eps, scale_factor=scale_factor, debug=debug, tb=tb
	)

	def loss(preds, theta_disp, pi_dropout, truth, tb_step: int = None):
		if debug:
			assert not (pi_dropout > 1.0).any()
			assert not (pi_dropout < 0.0).any()
		nb_case = nb_loss_func(preds, theta_disp, truth, tb_step=tb_step) - torch.log(
			1.0 - pi_dropout + eps
		)

		y_true = truth
		y_pred = preds * scale_factor
		theta = torch.clamp(theta_disp, max=1e6)

		zero_nb = torch.pow(theta / (theta + y_pred + eps), theta)
		zero_case = -torch.log(pi_dropout + ((1.0 - pi_dropout) * zero_nb) + eps)
		result = torch.where(y_true < 1e-8, zero_case, nb_case)

		# Ridge regularization on pi dropout term
		ridge = ridge_lambda * torch.pow(pi_dropout, 2)
		result += ridge

		# Total variation regularization on pi dropout term
		tv = tv_lambda * total_variation(pi_dropout)
		result += tv

		if tb is not None and tb_step is not None:
			tb.add_histogram("zinb/nb_case", nb_case, global_step=tb_step)
			tb.add_histogram("zinb/zero_nb", zero_nb, global_step=tb_step)
			tb.add_histogram("zinb/zero_case", zero_case, global_step=tb_step)
			tb.add_histogram("zinb/ridge", ridge, global_step=tb_step)
			tb.add_histogram("zinb/zinb_loss", result, global_step=tb_step)

		retval = torch.mean(result)
		# if debug:
		#     assert retval.item() > 0
		return retval

	return loss


def mmd(x, y):
	"""
	Compute maximum mean discrepancy

	References:
	https://ermongroup.github.io/blog/a-tutorial-on-mmd-variational-autoencoders/
	https://github.com/napsternxg/pytorch-practice/blob/master/Pytorch%20-%20MMD%20VAE.ipynb
	"""

	def compute_kernel(x, y):
		x_size = x.size(0)
		y_size = y.size(0)
		dim = x.size(1)
		x = x.unsqueeze(1)  # (x_size, 1, dim)
		y = y.unsqueeze(0)  # (1, y_size, dim)
		tiled_x = x.expand(x_size, y_size, dim)
		tiled_y = y.expand(x_size, y_size, dim)
		kernel_input = (tiled_x - tiled_y).pow(2).mean(2) / float(dim)
		return torch.exp(-kernel_input)  # (x_size, y_size)

	x_kernel = compute_kernel(x, x)
	y_kernel = compute_kernel(y, y)
	xy_kernel = compute_kernel(x, y)
	mmd = x_kernel.mean() + y_kernel.mean() - 2 * xy_kernel.mean()
	return mmd


def total_variation(x):
	"""
	Given a 2D input (where one dimension is a batch dimension, the actual values are
	one dimensional) compute the total variation (within a 1 position shift)
	"""
	t = torch.sum(torch.abs(x[:, :-1] - x[:, 1:]))
	return t


if __name__ == "__main__":
	MSELogLoss()

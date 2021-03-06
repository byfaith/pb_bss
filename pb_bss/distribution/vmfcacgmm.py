"""von-Mises-Fisher complex-Angular-Centric-Gaussian mixture model

This is a specific mixture model to integrate DC and spatial observations. It
does and will not support independent dimensions.

This also explains, why concrete variable names (i.e. F, T, embedding) are used.
"""
from operator import xor
from dataclasses import dataclass

import numpy as np

from pb_bss.distribution import VonMisesFisher
from pb_bss.distribution import VonMisesFisherTrainer
from pb_bss.distribution import (
    ComplexAngularCentralGaussian,
    ComplexAngularCentralGaussianTrainer,
)
from pb_bss.distribution.utils import _ProbabilisticModel
from pb_bss.utils import unsqueeze


@dataclass
class VMFCACGMM(_ProbabilisticModel):
    weight: np.array  # Shape (), (K,), (F, K), (T, K)
    weight_constant_axis: tuple
    vmf: VonMisesFisher
    cacg: ComplexAngularCentralGaussian
    spatial_weight: float
    spectral_weight: float

    def predict(self, observation, embedding):
        assert np.iscomplexobj(observation), observation.dtype
        assert np.isrealobj(embedding), embedding.dtype
        observation = observation / np.maximum(
            np.linalg.norm(observation, axis=-1, keepdims=True),
            np.finfo(observation.dtype).tiny,
        )
        embedding = embedding / np.maximum(
            np.linalg.norm(embedding, axis=-1, keepdims=True),
            np.finfo(embedding.dtype).tiny
        )
        affiliation, quadratic_form = self._predict(observation, embedding)
        return affiliation

    def _predict(self, observation, embedding):
        F, T, D = observation.shape
        _, _, E = embedding.shape

        observation_ = observation[..., None, :, :]
        cacg_log_pdf, quadratic_form = self.cacg._log_pdf(observation_)

        embedding_ = np.reshape(embedding, (1, F * T, E))
        vmf_log_pdf = self.vmf.log_pdf(embedding_)
        num_classes = vmf_log_pdf.shape[0]
        vmf_log_pdf = np.transpose(
            np.reshape(vmf_log_pdf, (num_classes, F, T)), (1, 0, 2)
        )

        affiliation = (
            unsqueeze(np.log(self.weight), self.weight_constant_axis)
            + self.spatial_weight * cacg_log_pdf
            + self.spectral_weight * vmf_log_pdf
        )
        affiliation -= np.max(affiliation, axis=-2, keepdims=True)
        np.exp(affiliation, out=affiliation)
        denominator = np.maximum(
            np.einsum("...kt->...t", affiliation)[..., None, :],
            np.finfo(affiliation.dtype).tiny,
        )
        affiliation /= denominator
        return affiliation, quadratic_form


class VMFCACGMMTrainer:
    def fit(
        self,
        observation,
        embedding,
        initialization=None,
        num_classes=None,
        iterations=100,
        saliency=None,
        min_concentration=1e-10,
        max_concentration=500,
        hermitize=True,
        trace_norm=True,
        eigenvalue_floor=1e-10,
        affiliation_eps=1e-10,
        weight_constant_axis=(-1,),
        spatial_weight=1.,
        spectral_weight=1.
    ) -> VMFCACGMM:
        """

        Args:
            observation: Shape (F, T, D)
            embedding: Shape (F, T, E)
            initialization: Affiliations between 0 and 1. Shape (F, K, T)
            num_classes: Scalar >0
            iterations: Scalar >0
            saliency: Importance weighting for each observation, shape (F, T)
            min_concentration:
            max_concentration:
            hermitize:
            trace_norm:
            eigenvalue_floor:
            affiliation_eps: Used in M-step to clip saliency.
            weight_constant_axis: Axis, along which weight is constant. The
                axis indices are based on affiliation shape. Consequently:
                (-3, -2, -1) == constant = ''
                (-3, -1) == 'k'
                (-1) == vanilla == 'fk'
                (-3) == 'kt'
            spatial_weight:
            spectral_weight:

        Returns:

        """
        assert xor(initialization is None, num_classes is None), (
            "Incompatible input combination. "
            "Exactly one of the two inputs has to be None: "
            f"{initialization is None} xor {num_classes is None}"
        )
        assert np.iscomplexobj(observation), observation.dtype
        assert np.isrealobj(embedding), embedding.dtype
        assert observation.shape[-1] > 1
        observation = observation / np.maximum(
            np.linalg.norm(observation, axis=-1, keepdims=True),
            np.finfo(observation.dtype).tiny,
        )

        F, T, D = observation.shape
        _, _, E = embedding.shape

        if initialization is None and num_classes is not None:
            affiliation_shape = (F, num_classes, T)
            initialization = np.random.uniform(size=affiliation_shape)
            initialization /= np.einsum("...kt->...t", initialization)[
                ..., None, :
            ]

        if saliency is None:
            saliency = np.ones_like(initialization[..., 0, :])

        quadratic_form = np.ones_like(initialization)
        affiliation = initialization
        for iteration in range(iterations):
            model = self._m_step(
                observation,
                embedding,
                quadratic_form,
                affiliation=np.clip(
                    affiliation, affiliation_eps, 1 - affiliation_eps
                ),
                saliency=saliency,
                min_concentration=min_concentration,
                max_concentration=max_concentration,
                hermitize=hermitize,
                trace_norm=trace_norm,
                eigenvalue_floor=eigenvalue_floor,
                weight_constant_axis=weight_constant_axis,
                spatial_weight=spatial_weight,
                spectral_weight=spectral_weight
            )

            if iteration < iterations - 1:
                affiliation, quadratic_form = model._predict(
                    observation=observation, embedding=embedding
                )

        return model

    def _m_step(
        self,
        observation,
        embedding,
        quadratic_form,
        affiliation,
        saliency,
        min_concentration,
        max_concentration,
        hermitize,
        trace_norm,
        eigenvalue_floor,
        weight_constant_axis,
        spatial_weight,
        spectral_weight
    ):
        F, T, D = observation.shape
        _, _, E = embedding.shape
        _, K, _ = affiliation.shape

        masked_affiliation = affiliation * saliency[..., None, :]

        if -2 in weight_constant_axis:
            weight = 1 / K
        else:
            weight = np.sum(
                masked_affiliation, axis=weight_constant_axis, keepdims=True
            )
            weight /= np.sum(weight, axis=-2, keepdims=True)
            weight = np.squeeze(weight, axis=weight_constant_axis)

        embedding_ = np.reshape(embedding, (1, F * T, E))
        masked_affiliation_ = np.reshape(
            np.transpose(masked_affiliation, (1, 0, 2)),
            (K, F * T)
        )  # 'fkt->k,ft'
        vmf = VonMisesFisherTrainer()._fit(
            y=embedding_,
            saliency=masked_affiliation_,
            min_concentration=min_concentration,
            max_concentration=max_concentration
        )
        cacg = ComplexAngularCentralGaussianTrainer()._fit(
            y=observation[..., None, :, :],
            saliency=masked_affiliation,
            quadratic_form=quadratic_form,
            hermitize=hermitize,
            trace_norm=trace_norm,
            eigenvalue_floor=eigenvalue_floor,
        )
        return VMFCACGMM(
            weight=weight,
            vmf=vmf,
            cacg=cacg,
            weight_constant_axis=weight_constant_axis,
            spatial_weight=spatial_weight,
            spectral_weight=spectral_weight
        )

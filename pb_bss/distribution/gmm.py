from operator import xor

import numpy as np
from dataclasses import dataclass
from sklearn.cluster import KMeans

from pb_bss.distribution import Gaussian, GaussianTrainer
from pb_bss.distribution.utils import _ProbabilisticModel
from pb_bss.utils import labels_to_one_hot


@dataclass
class GMM(_ProbabilisticModel):
    weight: np.array  # (..., K)
    gaussian: Gaussian

    def predict(self, x):
        *independent, num_observations, _ = x.shape

        affiliation = (
            np.log(self.weight)[..., :, None]
            + self.gaussian.log_pdf(x[..., None, :, :])
        )
        affiliation -= np.max(affiliation, axis=-2, keepdims=True)
        np.exp(affiliation, out=affiliation)
        denominator = np.maximum(
            np.einsum("...kn->...n", affiliation)[..., None, :],
            np.finfo(x.dtype).tiny,
        )
        affiliation /= denominator
        return affiliation


class GMMTrainer:
    def __init__(self, eps=1e-10):
        self.eps = eps
        self.log_likelihood_history = []

    def fit(
        self,
        y,
        initialization=None,
        num_classes=None,
        iterations=100,
        saliency=None,
        covariance_type="full",
    ):
        """

        Args:
            y: Shape (..., N, D)
            initialization: Affiliations between 0 and 1. Shape (..., K, N)
            num_classes: Scalar >0
            iterations: Scalar >0
            saliency: Importance weighting for each observation, shape (..., N)
            covariance_type: Either 'full', 'diagonal', or 'spherical'

        Returns:

        TODO: Support different weight types
        """
        assert xor(initialization is None, num_classes is None), (
            "Incompatible input combination. "
            "Exactly one of the two inputs has to be None: "
            f"{initialization is None} xor {num_classes is None}"
        )
        assert np.isrealobj(y), y.dtype

        if initialization is None and num_classes is not None:
            *independent, num_observations, _ = y.shape
            affiliation_shape = (*independent, num_classes, num_observations)
            initialization = np.random.uniform(size=affiliation_shape)
            initialization /= np.einsum("...kn->...n", initialization)[
                ..., None, :
            ]

        if saliency is None:
            saliency = np.ones_like(initialization[..., 0, :])

        return self._fit(
            y,
            initialization=initialization,
            iterations=iterations,
            saliency=saliency,
            covariance_type=covariance_type,
        )

    def _fit(self, y, initialization, iterations, saliency, covariance_type):
        affiliation = initialization  # TODO: Do we need np.copy here?
        for iteration in range(iterations):
            model = self._m_step(
                y,
                affiliation=affiliation,
                saliency=saliency,
                covariance_type=covariance_type,
            )

            if iteration < iterations - 1:
                affiliation = model.predict(y)

        return model

    def _m_step(self, x, affiliation, saliency, covariance_type):
        masked_affiliation = affiliation * saliency[..., None, :]
        weight = np.einsum("...kn->...k", masked_affiliation)
        weight /= np.einsum("...n->...", saliency)[..., None]

        gaussian = GaussianTrainer()._fit(
            y=x[..., None, :, :],
            saliency=masked_affiliation,
            covariance_type=covariance_type
        )
        return GMM(weight=weight, gaussian=gaussian)


@dataclass
class BinaryGMM(_ProbabilisticModel):
    kmeans: KMeans  # from sklearn

    def predict(self, x):
        """

        Args:
            x: Shape (N, D)

        Returns: Affiliation with shape (K, N)

        """
        N, D = x.shape
        assert np.isrealobj(x), x.dtype

        labels = self.kmeans.predict(x)
        affiliations = labels_to_one_hot(
            labels, self.kmeans.n_clusters, axis=-2, keepdims=False,
            dtype=x.dtype
        )
        assert affiliations.shape == (self.kmeans.n_clusters, N)
        return affiliations


class BinaryGMMTrainer:
    """
    This is a specific wrapper of sklearn's kmeans for Deep Clustering
    embeddings. This explains the variable names and also the fixed shape for
    the embeddings.



    Returns:
    """
    def fit(
        self,
        x,
        num_classes,
        saliency=None
    ):
        """

        Args:
            x: Shape (N, D)
            num_classes: Scalar >0
            saliency: Importance weighting for each observation, shape (N,)
                Saliency has to be boolean.

        """
        N, D = x.shape
        if saliency is not None:
            assert saliency.dtype == np.bool, (
                'Only boolean saliency supported. '
                f'Current dtype: {saliency.dtype}.'
            )
            assert saliency.shape == (N,)
            x = x[saliency, :]
        return BinaryGMM(kmeans=KMeans(n_clusters=num_classes).fit(x))

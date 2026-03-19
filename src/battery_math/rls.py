"""Scalar Recursive Least Squares (RLS) estimator with forgetting factor.

Used for online calibration of battery model parameters (ir_k, Peukert exponent).
Pure math kernel — no I/O, no logging.
"""

from __future__ import annotations


class ScalarRLS:
    """Scalar RLS estimator: tracks a single parameter with exponential forgetting.

    φ=1 scalar model: measurement = theta + noise.
    Forgetting factor λ controls how fast old data decays (0.95–0.99 typical).
    """

    def __init__(self, theta: float, P: float = 1.0, forgetting_factor: float = 0.97):
        """Initialize RLS estimator. Callers must clamp theta after each update() call."""
        self.theta = theta
        self.P = P
        self.forgetting_factor = forgetting_factor
        self.sample_count = 0

    def update(self, measurement: float) -> tuple[float, float]:
        """Feed one measurement; mutates self.theta and self.P in-place AND returns them.

        The returned (theta, P) are the same values as self.theta/self.P after
        mutation — callers may use either the return value or read the attributes.

        P is the error covariance scalar — starts at 1.0 (high uncertainty),
        decreases toward 0 as confidence grows. Callers MUST clamp theta
        after update to physical bounds (e.g., Peukert [1.0, 1.5], ir_k [0, 0.1]).
        Skipping clamping allows physically invalid estimates that the RLS will
        not self-correct. Clamping creates minor P/theta inconsistency bounded
        by the narrow clamp ranges — P evolves self-consistently from the
        pre-clamp state; only theta is shifted, and K on the next call remains
        well-calibrated.
        """
        K = self.P / (self.forgetting_factor + self.P)
        self.theta += K * (measurement - self.theta)
        self.P = (1 - K) * self.P / self.forgetting_factor
        self.sample_count += 1
        return self.theta, self.P

    @property
    def confidence(self) -> float:
        """Confidence metric: 0.0 (no data) → 1.0 (converged). Derived from P."""
        return 1.0 / (1.0 + self.P)

    def to_dict(self) -> dict:
        """Serialize state for model.json persistence."""
        return {
            'theta': self.theta,
            'P': self.P,
            'sample_count': self.sample_count,
            'forgetting_factor': self.forgetting_factor,
        }

    @classmethod
    def from_dict(cls, d: dict, forgetting_factor: float = 0.97) -> ScalarRLS:
        """Restore from serialized dict. Missing keys → sensible defaults."""
        obj = cls(
            theta=d.get('theta', 0.0),
            P=d.get('P', 1.0),
            forgetting_factor=d.get('forgetting_factor', forgetting_factor),
        )
        obj.sample_count = d.get('sample_count', 0)
        return obj

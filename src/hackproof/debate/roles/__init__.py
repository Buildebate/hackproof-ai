"""Six-role asymmetric debate pipeline (Step 2.2).

Pipeline order is fixed: EdgeScout -> ArchitecturalBuilder ->
OccamsRazorCritic + SignatureFactChecker -> FormalAdversaryCritic ->
ConsensusJudge. Spark roles run on the injected Spark client, Wonder
roles on the injected Wonder client (mocks in unit tests).
"""

from hackproof.debate.roles import (
    architectural_builder,
    consensus_judge,
    edge_scout,
    formal_adversary_critic,
    occams_razor_critic,
    signature_fact_checker,
)

ROLES = (
    edge_scout,
    architectural_builder,
    occams_razor_critic,
    signature_fact_checker,
    formal_adversary_critic,
    consensus_judge,
)
ROLE_ORDER = tuple(role.ROLE_NAME for role in ROLES)

__all__ = [
    "ROLES",
    "ROLE_ORDER",
    "edge_scout",
    "architectural_builder",
    "occams_razor_critic",
    "signature_fact_checker",
    "formal_adversary_critic",
    "consensus_judge",
]

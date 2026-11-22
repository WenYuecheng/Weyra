"""Whole-file profile retaining original contracts and auditing the full repair."""

from dataclasses import replace

from . import glm_campaign as campaign
from . import glm_full, glm_public

REPAIR_REVIEW = (
    "Use the starter as the authority for names, signatures, exports and return shapes. "
    "Inventory all reported failure behaviors before editing; a compiler error can hide "
    "additional logical defects. After fixing an explicit error, trace the affected public "
    "methods and invariants against the contract and diagnostic examples. Preserve correct "
    "behavior and avoid speculative rewrites. Check that every intended fix is actually "
    "present in the complete code you send to write_file."
)


def configure():
    """Keep the first-request skill shared, and strengthen only Weyra's repair context."""
    glm_public.configure()
    glm_full.PROFILE = replace(
        glm_full.PROFILE, repair_context="contract", repair_review=REPAIR_REVIEW,
    )
    glm_full.WEYRA_SKILL = campaign.BASE_SKILL


def main():
    """Run the candidate through the same fixed-corpus, metered CLI."""
    configure()
    glm_full.main()


if __name__ == "__main__":
    main()

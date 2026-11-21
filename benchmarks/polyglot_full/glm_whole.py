"""Two-submission, whole-file profile for a bounded GLM Polyglot campaign."""

from . import glm_campaign as campaign
from . import glm_full


def configure():
    """Give both arms one complete file-writing response per submission."""
    glm_full.PROFILE = campaign.InferenceSettings(
        context_window=1_000_000,
        output_tokens=8_192,
        reasoning_effort="high",
        temperature=1.0,
        top_p=0.95,
        steps=1,
        seconds=600,
    )
    glm_full.TASK_TOKENS = 65_536
    glm_full.TASK_CALLS = 2
    campaign.TOOL_NAMES = ("write_file",)
    campaign.SYSTEM = (
        "Implement the exercise and save the complete implementation with write_file. "
        "The entire original contents of every editable file are supplied. "
        "You have exactly one model response per submission: in this response call write_file "
        "for every file that must change. Batch all required writes in this one response. "
        "Only files explicitly designated editable may be written. "
        "Preserve the supplied public interface and implement all required behavior. "
        "Use concise reasoning so the complete code fits the output budget. "
        "Independent tests run immediately after your tool calls; no acknowledgement is needed."
    )
    glm_full.WEYRA_SKILL = (
        campaign.BASE_SKILL
        + " Before submitting, reason through the stated examples, empty inputs, boundaries, "
        "output formatting and state transitions. Check the implementation against the exact "
        "interface, then save all required files in this response."
    )


def main():
    """Use the same inventory, preflight, metering and reporting CLI as glm_full."""
    configure()
    glm_full.main()


if __name__ == "__main__":
    main()

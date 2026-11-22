"""Whole-file comparison with standard public test-runner feedback."""

from dataclasses import replace

from . import glm_full, glm_whole


def configure():
    """Freeze test-output feedback identically for both arms."""
    glm_whole.configure()
    glm_full.PROFILE = replace(glm_full.PROFILE, feedback_mode="test_output")


def main():
    """Run the independent public-feedback profile through the shared CLI."""
    configure()
    glm_full.main()


if __name__ == "__main__":
    main()

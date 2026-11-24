"""Fixed first-submission and repair prompts for the paired experiment."""

import json

FIRST_SKILL = (
    "Before writing, trace one stated example and one boundary implied by the instructions. "
    "Check names, argument and return types, exports, and imports against the stated requirements "
    "and examples; starter stubs can be incomplete. Keep that check brief. Call write_file "
    "with the complete implementation of every changed editable file in this response."
)
REPAIR_ACTION = (
    "Repair the current implementation using the verifier evidence below. "
    "Use concrete verifier diagnostics to resolve interface or type mismatches, even when a "
    "starter placeholder suggests otherwise. Preserve behavior that already passes. "
    "Call write_file with complete changed files in this response."
)
BASELINE_ACTION = (
    "The verifier found failures. Repair the existing implementation and save files. "
    "Preserve the public interface and handle the general case.\n"
)


def first(task):
    initial = "\n\n".join(f"File {path}:\n{task.files[path]}" for path in task.editable)
    return f"{task.instruction}\n\n{initial}\n\nSave work only in: {', '.join(task.editable)}."


def compact(task, files, diagnostic):
    if set(files) != set(task.editable) or not set(task.editable) <= set(task.files):
        raise ValueError("Repair requires the complete editable inventory")
    if any(type(raw) is not bytes for raw in files.values()):
        raise ValueError("Repair requires byte-exact editable files")
    current = {name: files[name].decode("utf-8") for name in task.editable}
    starters = {
        name: task.files[name] for name in task.editable
        if files[name] != task.files[name].encode("utf-8")
    }
    return "\n\n".join((
        REPAIR_ACTION,
        "Original instruction\n" + task.instruction,
        "Editable paths\n" + encode(list(task.editable)),
        "Current saved implementation (JSON)\n" + encode(current),
        "Original starter context (may be incomplete; reconcile with requirements and verifier)\n"
        + encode(starters),
        "Latest official diagnostic\n" + diagnostic,
    ))


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

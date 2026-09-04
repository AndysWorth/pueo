import importlib.resources

_cache: dict[str, str] = {}
# Traversable pointing at the prompts/ package; tests inject a tmp_path Path here.
_prompt_ref = importlib.resources.files("prompts")


def load_prompt(name: str, /, **kwargs: str) -> str:
    """Load a prompt template from prompts/<name>.md, caching after first read."""
    if name not in _cache:
        _cache[name] = _prompt_ref.joinpath(f"{name}.md").read_text(encoding="utf-8")
    text = _cache[name]
    return text.format_map(kwargs) if kwargs else text


def repeat_query(system_prompt: str, user_content: str) -> str:
    """Append the system prompt to the end of user_content after a separator.

    Prevents "lost in the middle" failures on long contexts: when the model's
    attention drifts away from the instruction, seeing it again at the end of
    the content anchors the response to the task.  Adds ~1–2× the system prompt
    overhead to the token count.
    """
    return f"{user_content}\n\n---\n\n{system_prompt}"

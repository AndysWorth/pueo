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

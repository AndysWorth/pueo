from paths import get_dirs as _get_dirs

_PROMPT_DIR = _get_dirs().resources_dir / "prompts"
_cache: dict[str, str] = {}


def load_prompt(name: str, /, **kwargs: str) -> str:
    """Load a prompt template from prompts/<name>.md, caching after first read."""
    if name not in _cache:
        _cache[name] = (_PROMPT_DIR / f"{name}.md").read_text()
    text = _cache[name]
    return text.format_map(kwargs) if kwargs else text

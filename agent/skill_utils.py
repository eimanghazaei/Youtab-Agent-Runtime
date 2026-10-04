"""Skill runtime helpers and compatibility exports for canonical metadata.

Metadata and raw-config discovery live in :mod:`agent.skill_metadata`, below
runtime/tool dependencies. Adapters preserve the established patch seams for
callers while sharing that implementation and its caches.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

from youtab_constants import get_config_path, get_skills_dir, is_termux
from agent import skill_metadata as _metadata
from agent.skill_metadata import (
    EXCLUDED_SKILL_DIRS as EXCLUDED_SKILL_DIRS,
    SKILL_SUPPORT_DIRS as SKILL_SUPPORT_DIRS,
    PLATFORM_MAP as PLATFORM_MAP,
    ORG_MIRROR_DIR_NAME as ORG_MIRROR_DIR_NAME,
    ORG_ACTIVE_MARKER as ORG_ACTIVE_MARKER,
    ORG_PROVENANCE_FILE as ORG_PROVENANCE_FILE,
    ORG_BASELINE_FILE as ORG_BASELINE_FILE,
    SKILL_CONFIG_PREFIX as SKILL_CONFIG_PREFIX,
    SKILL_PROMPT_DESC_LIMIT as SKILL_PROMPT_DESC_LIMIT,
    _RAW_CONFIG_CACHE as _RAW_CONFIG_CACHE,
    _EXTERNAL_DIRS_CACHE as _EXTERNAL_DIRS_CACHE,
    _raw_config_cache_clear as _raw_config_cache_clear,
    _external_dirs_cache_clear as _external_dirs_cache_clear,
    _normalize_string_set as _normalize_string_set,
    _resolve_dotpath as _resolve_dotpath,
    _resolve_for_skill_ownership as _resolve_for_skill_ownership,
    _normalize_skill_description as _normalize_skill_description,
    read_active_org_id as read_active_org_id,
    is_org_mirror_path as is_org_mirror_path,
    org_id_of_path as org_id_of_path,
    is_excluded_skill_path as is_excluded_skill_path,
    is_skill_support_path as is_skill_support_path,
    yaml_load as yaml_load,
    extract_skill_conditions as extract_skill_conditions,
    extract_skill_config_vars as extract_skill_config_vars,
    extract_skill_description as extract_skill_description,
    is_skill_description_truncated_for_prompt as is_skill_description_truncated_for_prompt,
    iter_skill_index_files as iter_skill_index_files,
    parse_qualified_name as parse_qualified_name,
    is_valid_namespace as is_valid_namespace,
)

logger = logging.getLogger(__name__)


def parse_frontmatter(content: str):
    return _metadata.parse_frontmatter(content, _yaml_loader=yaml_load)


def skill_matches_platform_list(platforms: Any) -> bool:
    return _metadata.skill_matches_platform_list(
        platforms, _current_platform=sys.platform, _running_in_termux=is_termux(),
    )


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    return skill_matches_platform_list(frontmatter.get("platforms"))


def _load_raw_config() -> Dict[str, Any]:
    return _metadata._load_raw_config(
        _config_path=get_config_path(), _yaml_loader=yaml_load,
    )


def get_disabled_skill_names(platform: str | None = None) -> Set[str]:
    return _metadata.get_disabled_skill_names(platform, _config_loader=_load_raw_config)


def get_external_skills_dirs() -> List[Path]:
    return _metadata.get_external_skills_dirs(
        _config_path=get_config_path(), _config_loader=_load_raw_config,
    )


def get_all_skills_dirs() -> List[Path]:
    return _metadata.get_all_skills_dirs(_external_dirs_loader=get_external_skills_dirs)


def is_external_skill_path(path) -> bool:
    return _metadata.is_external_skill_path(path, _external_dirs_loader=get_external_skills_dirs)


def discover_all_skill_config_vars() -> List[Dict[str, Any]]:
    return _metadata.discover_all_skill_config_vars(
        _disabled_loader=get_disabled_skill_names,
        _dirs_loader=get_all_skills_dirs,
        _frontmatter_parser=parse_frontmatter,
        _platform_matcher=skill_matches_platform,
    )


def resolve_skill_config_values(config_vars: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _metadata.resolve_skill_config_values(config_vars, _config_loader=_load_raw_config)


# ── Environment matching ──────────────────────────────────────────────────

# Recognized environment tags and how each is detected. An environment tag is
# a *relevance* gate, not a hard-compatibility gate (that is what ``platforms:``
# is for). A skill tagged for an environment it isn't relevant to is hidden from
# the skills index / offer surfaces so it does not add noise for users who will
# never need it — but it can ALWAYS still be loaded explicitly (``skill_view``,
# ``--skills``), because an explicit request is explicit consent.
#
# Detection is cached for the process lifetime via ``_ENV_DETECT_CACHE``.
_KNOWN_ENVIRONMENTS = frozenset({"kanban", "docker", "s6"})

_ENV_DETECT_CACHE: Dict[str, bool] = {}


def _detect_environment(env: str) -> bool:
    """Return True when the named runtime environment is currently active.

    Cached per process. Unknown env names return True (fail-open: never hide a
    skill because of a tag we don't understand).
    """
    if env in _ENV_DETECT_CACHE:
        return _ENV_DETECT_CACHE[env]

    result = True
    if env == "kanban":
        # Kanban is "active" either as a dispatcher-spawned worker (the
        # dispatcher sets ``YOUTAB_AGENT_KANBAN_TASK`` / ``YOUTAB_AGENT_KANBAN_BOARD`` in the
        # worker env) or as an orchestrator profile that has opted into the
        # kanban toolset. Mirror the same signals the kanban tools themselves
        # gate on (``tools/kanban_tools.py``) so the offer filter agrees with
        # tool availability.
        if os.getenv("YOUTAB_AGENT_KANBAN_TASK") or os.getenv("YOUTAB_AGENT_KANBAN_BOARD"):
            result = True
        else:
            try:
                from tools.kanban_tools import _profile_has_kanban_toolset

                result = bool(_profile_has_kanban_toolset())
            except Exception:
                result = False
    elif env == "docker":
        try:
            from youtab_constants import is_container

            result = is_container()
        except Exception:
            result = False
    elif env == "s6":
        # The Youtab Docker image runs s6-overlay as PID 1 (/init). s6 plants
        # its runtime scaffolding under /run/s6 and ships its admin tree under
        # /package/admin/s6-overlay. Either marker means we're inside an
        # s6-supervised container.
        result = os.path.isdir("/run/s6") or os.path.isdir(
            "/package/admin/s6-overlay"
        )

    _ENV_DETECT_CACHE[env] = result
    return result


def skill_matches_environment(frontmatter: Dict[str, Any]) -> bool:
    """Return True when the skill is relevant to the current runtime environment.

    Skills may declare an ``environments`` list in their YAML frontmatter::

        environments: [kanban]        # only relevant when kanban is active
        environments: [s6]            # only relevant inside the s6 Docker image
        environments: [docker]        # only relevant inside any container

    If the field is absent or empty the skill is relevant in **all**
    environments (backward-compatible default).

    This is an OFFER-time filter: it controls whether a skill shows up in the
    skills index / autocomplete / slash-command list. It is intentionally NOT
    enforced by ``skill_view`` or ``--skills`` preloading — an explicit load is
    explicit consent, and load-bearing force-loads (e.g. a dispatcher pinning
    a task to a specialist skill via ``--skills``) must always succeed
    regardless of how the offer surfaces filter the skill.

    A skill matches when ANY of its declared environments is currently active
    (OR semantics, mirroring ``platforms``). Unknown env tags fail open.
    """
    environments = frontmatter.get("environments")
    if not environments:
        return True
    if not isinstance(environments, list):
        environments = [environments]
    for env in environments:
        normalized = str(env).lower().strip()
        if not normalized:
            continue
        if normalized not in _KNOWN_ENVIRONMENTS:
            # Tag we don't understand — don't hide the skill over it.
            return True
        if _detect_environment(normalized):
            return True
    return False


def normalize_skill_lookup_name(identifier: str) -> str:
    """Normalize a skill identifier to a ``skill_view()``-safe relative path.

    Slash commands and cron jobs may store absolute paths to skills that live
    under ``~/.youtab-agent-runtime/skills/`` (including via symlinks) or configured
    ``skills.external_dirs``. ``skill_view()`` rejects absolute names for
    security, so callers must translate trusted absolute paths to their
    relative form first.
    """
    raw_identifier = (identifier or "").strip()
    if not raw_identifier:
        return raw_identifier

    identifier_path = Path(raw_identifier).expanduser()
    if not identifier_path.is_absolute():
        return raw_identifier.lstrip("/")

    # Look the primary skills root up on tools.skills_tool at CALL time
    # (not via get_skills_dir()): callers and tests patch
    # ``tools.skills_tool.SKILLS_DIR`` and skill_view() itself resolves
    # against that module attribute, so normalization must agree with the
    # exact root skill_view() will enforce.  Import deferred to avoid a
    # module cycle (tools.skills_tool imports agent.skill_utils).
    try:
        from tools import skills_tool as _skills_tool
        primary_root = Path(_skills_tool.SKILLS_DIR)
    except Exception:
        primary_root = get_skills_dir()

    trusted_roots = [primary_root]
    try:
        trusted_roots.extend(get_external_skills_dirs())
    except Exception:
        pass

    # Prefer the lexical path under a trusted skill root before resolving
    # symlinks. Slash-command discovery can legitimately find a skill via
    # ~/.youtab-agent-runtime/skills/<name> where <name> is a symlink to a checked-out
    # skill elsewhere. Resolving first turns that trusted visible path into
    # an arbitrary absolute path that skill_view() refuses to load.
    for root in trusted_roots:
        try:
            return str(identifier_path.relative_to(root))
        except ValueError:
            continue

    try:
        return str(identifier_path.resolve().relative_to(primary_root.resolve()))
    except Exception:
        logger.debug(
            "Skill identifier %r is an absolute path outside trusted skills "
            "roots — passing through unchanged (skill_view will reject it)",
            raw_identifier,
        )
        return raw_identifier

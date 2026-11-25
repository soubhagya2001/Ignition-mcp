"""Local Ignition install reader.

When Ignition runs on the same machine, the MCP can read its configuration and project
resources straight from disk — no HTTP, no credentials. This module is the single source of
truth for local access; tools call it through :attr:`ServerState.local_reader`.

Directory layout used (Ignition 8.1+):

.. code-block:: text

    <install>/data/gateway.xml                      # ports / SSL settings
    <install>/data/db/config.idb                    # SQLite gateway config
    <install>/logs/wrapper.log                      # system log
    <install>/data/projects/<project>/project.json  # project metadata + parent
    .../com.inductiveautomation.perspective/views/<view>/view.json
    .../ignition/script-python/<module>/code.py
    .../com.inductiveautomation.sqltables/named-query/<query>/query.json
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ignition_mcp.safety import snapshot_file

logger = logging.getLogger(__name__)

PERSPECTIVE_MODULE = "com.inductiveautomation.perspective"
SCRIPT_PYTHON_DIR = Path("ignition") / "script-python"
NAMED_QUERY_MODULE = "com.inductiveautomation.sqltables"
NAMED_QUERY_DIR = Path("named-query")


@dataclass
class LocalInstallInfo:
    """Configuration discovered from a local Ignition install directory.

    Attributes:
        install_path: Install root path.
        gateway_url: Best-guess gateway URL derived from the configured ports.
        http_port: HTTP port (8088 by default).
        https_port: HTTPS port (8043 by default).
        ssl_enabled: Whether the gateway redirects to HTTPS.
        projects: Project names found on disk.
        db_connections: Database connection names from ``config.idb``.
        installed_modules: Installed module names from ``config.idb``.
        gateway_version: Version string when it can be determined.
    """

    install_path: str = ""
    gateway_url: str | None = None
    http_port: int = 8088
    https_port: int = 8043
    ssl_enabled: bool = False
    projects: list[str] = field(default_factory=list)
    db_connections: list[str] = field(default_factory=list)
    installed_modules: list[str] = field(default_factory=list)
    gateway_version: str = ""


class LocalInstallReader:
    """Read-only access to a local Ignition install and its project resources.

    Args:
        install_path: Root of the Ignition installation (the directory containing ``data``).
    """

    def __init__(self, install_path: Path) -> None:
        self.root = Path(install_path)
        self.data_dir = self.root / "data"
        self.projects_dir = self.data_dir / "projects"
        self.logs_dir = self.root / "logs"
        self.config_db = self.data_dir / "db" / "config.idb"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def read(self) -> LocalInstallInfo:
        """Read gateway settings, the config database and the project list.

        Returns:
            A populated :class:`LocalInstallInfo`. Missing files are skipped, never raised.
        """
        info = LocalInstallInfo(install_path=str(self.root))
        self._read_gateway_xml(info)
        await self._read_config_db(info)
        self._read_projects_dir(info)
        return info

    def _read_gateway_xml(self, info: LocalInstallInfo) -> None:
        """Parse ``data/gateway.xml`` (or ``ignition.conf``) for HTTP/HTTPS ports."""
        settings = self._gateway_settings()
        http_port = _first_int(settings, ("httpport", "http.port", "gateway.port", "port"))
        https_port = _first_int(settings, ("httpsport", "https.port", "sslport", "secureport"))
        ssl_enabled = _first_bool(settings, ("sslenabled", "ssl.enabled", "gateway.ssl", "ssl"))

        if http_port:
            info.http_port = http_port
        if https_port:
            info.https_port = https_port
        info.ssl_enabled = ssl_enabled

        if ssl_enabled:
            info.gateway_url = f"https://localhost:{info.https_port}"
        else:
            info.gateway_url = f"http://localhost:{info.http_port}"

    def _gateway_settings(self) -> dict[str, str]:
        """Flatten ``gateway.xml`` and ``ignition.conf`` into a lowercase key/value map."""
        settings: dict[str, str] = {}
        gateway_xml = self.data_dir / "gateway.xml"
        if gateway_xml.exists():
            try:
                root = ET.parse(gateway_xml).getroot()
                for element in root.iter():
                    tag = _normalise_key(element.tag)
                    text = (element.text or "").strip()
                    if text:
                        settings.setdefault(tag, text)
                    for key, value in element.attrib.items():
                        settings[_normalise_key(key)] = value
                        # Ignition stores settings as <entry key="..." value="..."/>.
                        if _normalise_key(key) == "key" and value:
                            entry_value = element.attrib.get("value", "")
                            settings[_normalise_key(value)] = entry_value
            except (ET.ParseError, OSError) as exc:
                logger.debug("Could not parse %s: %s", gateway_xml, exc)

        ignition_conf = self.root / "ignition.conf"
        if ignition_conf.exists():
            try:
                text = ignition_conf.read_text(encoding="utf-8", errors="replace")
                for match in re.finditer(
                    r"(?i)([a-z0-9_.-]*(?:http|ssl|port)[a-z0-9_.-]*)\s*[=:]\s*([^\s]+)",
                    text,
                ):
                    settings.setdefault(_normalise_key(match.group(1)), match.group(2))
            except OSError as exc:
                logger.debug("Could not read %s: %s", ignition_conf, exc)
        return settings

    async def _read_config_db(self, info: LocalInstallInfo) -> None:
        """Read project, connection and module names from ``config.idb`` (SQLite)."""
        if not self.config_db.exists():
            logger.debug("config.idb not found at %s", self.config_db)
            return
        try:
            import aiosqlite
        except ImportError:  # pragma: no cover - aiosqlite is a declared dependency
            logger.debug("aiosqlite not installed — skipping config.idb read")
            return

        try:
            # Open read-only so the MCP can never mutate gateway configuration.
            uri = f"file:{self.config_db.as_posix()}?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as db:
                info.projects = await _distinct_names(db, ("PROJECT",)) or info.projects
                info.db_connections = await _distinct_names(db, ("DBCONN", "CONNECTION"))
                info.installed_modules = await _distinct_names(db, ("MODULE",))
        except Exception as exc:
            logger.debug("Could not read config.idb: %s", exc)

    def _read_projects_dir(self, info: LocalInstallInfo) -> None:
        """Fill in project names from ``data/projects`` when the database had none."""
        if not self.projects_dir.exists():
            return
        found = sorted(
            child.name
            for child in self.projects_dir.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and (child / "project.json").is_file()
        )
        if found and not info.projects:
            info.projects = found

    # ------------------------------------------------------------------
    # Path safety and project metadata
    # ------------------------------------------------------------------

    @staticmethod
    def safe_path(base: Path, *parts: str) -> Path | None:
        """Join ``parts`` onto ``base``, refusing paths that escape it.

        Args:
            base: Directory that must contain the result.
            *parts: Path fragments; ``/`` and ``\\`` separators are both accepted.

        Returns:
            The resolved path, or ``None`` if it would escape ``base``.
        """
        fragments: list[str] = []
        for part in parts:
            fragments.extend(piece for piece in re.split(r"[\\/]+", str(part)) if piece)
        try:
            candidate = base.joinpath(*fragments).resolve()
            resolved_base = base.resolve()
        except (OSError, RuntimeError):
            return None
        if candidate != resolved_base and resolved_base not in candidate.parents:
            return None
        return candidate

    def project_path(self, project: str) -> Path:
        """Return the directory for a project (may not exist)."""
        safe = self.safe_path(self.projects_dir, project)
        if safe is not None:
            return safe
        return self.projects_dir / "__invalid_project__"

    def create_project(
        self,
        name: str,
        *,
        title: str | None = None,
        description: str | None = None,
        parent: str | None = None,
        enabled: bool = True,
        inheritable: bool = False,
    ) -> tuple[Path | None, dict[str, str]]:
        """Create a blank local Ignition project directory and metadata.

        Args:
            name: Project directory name.
            title: Human-readable project title.
            description: Optional project description.
            parent: Optional direct parent project name.
            enabled: Whether the project should be enabled.
            inheritable: Whether the project can be inherited by child projects.

        Returns:
            A tuple of the project path and validation issues.
        """
        issues: dict[str, str] = {}
        if not name or name.strip() != name or "/" in name or "\\" in name or name in (".", ".."):
            issues["name"] = "Project name must be a single valid directory name."
            return None, issues

        project_dir = self.safe_path(self.projects_dir, name)
        if project_dir is None:
            issues["name"] = "Project name escapes the projects directory."
            return None, issues
        if project_dir.exists():
            issues["name"] = "A project with this name already exists."
            return None, issues
        if parent:
            parent_dir = self.safe_path(self.projects_dir, parent)
            if parent_dir is None or not parent_dir.exists():
                issues["parent"] = f"Parent project '{parent}' was not found locally."
                return None, issues

        try:
            project_dir.mkdir(parents=True)
            (project_dir / PERSPECTIVE_MODULE).mkdir()
            (project_dir / PERSPECTIVE_MODULE / "views").mkdir()
            (project_dir / PERSPECTIVE_MODULE / "page-config").mkdir()
            (project_dir / "ignition").mkdir()
            (project_dir / SCRIPT_PYTHON_DIR).mkdir()
            (project_dir / NAMED_QUERY_MODULE).mkdir()
            (project_dir / NAMED_QUERY_MODULE / NAMED_QUERY_DIR).mkdir()

            metadata: dict[str, Any] = {
                "title": title or name,
                "enabled": enabled,
                "inheritable": inheritable,
                "parent": parent,
            }
            if description:
                metadata["description"] = description
            (project_dir / "project.json").write_text(
                json.dumps(metadata, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            issues["filesystem"] = str(exc)
            return None, issues
        return project_dir, issues

    def list_projects(self) -> list[str]:
        """Return valid Ignition project directory names on disk (containing ``project.json``)."""
        if not self.projects_dir.exists():
            return []
        return sorted(
            child.name
            for child in self.projects_dir.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and (child / "project.json").is_file()
        )

    def read_project_meta(self, project: str) -> dict[str, Any]:
        """Read ``project.json`` for a project.

        Args:
            project: Project name.

        Returns:
            The decoded metadata, or an empty dict when unavailable.
        """
        path = self.safe_path(self.project_path(project), "project.json")
        if not path or not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("Could not read project.json for %s: %s", project, exc)
            return {}

    def inheritance_chain(self, project: str, max_depth: int = 10) -> list[str]:
        """Return ``[project, parent, grandparent, ...]`` for a project.

        Args:
            project: Project name.
            max_depth: Safety limit to avoid cycles.

        Returns:
            The inheritance chain, starting with the project itself.
        """
        chain = [project]
        current = project
        for _ in range(max_depth):
            parent = self.read_project_meta(current).get("parent")
            if not parent or not isinstance(parent, str) or parent in chain:
                break
            chain.append(parent)
            current = parent
        return chain

    # ------------------------------------------------------------------
    # Perspective resources
    # ------------------------------------------------------------------

    def _views_root(self, project: str) -> Path:
        """Return the Perspective views directory for a project."""
        return self.project_path(project) / PERSPECTIVE_MODULE / "views"

    def list_perspective_views(self, project: str) -> list[str]:
        """List Perspective view paths in a project.

        Returns:
            View paths such as ``["Dashboard/Overview", "Equipment/Pump"]``.
        """
        views_root = self._views_root(project)
        if not views_root.exists():
            return []
        return sorted(
            view_file.parent.relative_to(views_root).as_posix()
            for view_file in views_root.rglob("view.json")
        )

    def read_perspective_view(self, project: str, view_path: str) -> dict[str, Any] | None:
        """Read a Perspective view's JSON definition.

        Args:
            project: Project name.
            view_path: View path such as ``Dashboard/Overview``.

        Returns:
            The decoded view JSON, or ``None`` when the view does not exist.
        """
        view_file = self.safe_path(self._views_root(project), view_path, "view.json")
        if not view_file or not view_file.exists():
            return None
        try:
            return json.loads(view_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("Could not read view %s/%s: %s", project, view_path, exc)
            return None

    def write_perspective_view(
        self, project: str, view_path: str, view: dict[str, Any]
    ) -> Path | None:
        """Write a Perspective view JSON file.

        Args:
            project: Project name.
            view_path: Target view path.
            view: Full view JSON document.

        Returns:
            The written path, or ``None`` when the path is invalid.
        """
        view_file = self.safe_path(self._views_root(project), view_path, "view.json")
        if not view_file:
            return None
        if view_file.exists():
            snapshot_file(view_file, "write_perspective_view", project=project, view=view_path)
        view_file.parent.mkdir(parents=True, exist_ok=True)
        view_file.write_text(json.dumps(view, indent=2, sort_keys=False), encoding="utf-8")
        return view_file

    def read_page_config(self, project: str) -> dict[str, Any] | None:
        """Read the Perspective page configuration for a project."""
        base = self.project_path(project) / PERSPECTIVE_MODULE
        for candidate in ("page-config/config.json", "page-config.json"):
            path = self.safe_path(base, candidate)
            if path and path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None
        return None

    def list_style_classes(self, project: str) -> list[str]:
        """List style class names defined in a project."""
        return sorted(self._style_class_files(project))

    def read_style_classes(self, project: str) -> dict[str, Any]:
        """Read every style class definition in a project.

        Returns:
            Mapping of style class name to its decoded definition.
        """
        styles: dict[str, Any] = {}
        base = self.project_path(project) / PERSPECTIVE_MODULE
        for name, path in self._style_class_files(project).items():
            try:
                styles[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        theme = self.safe_path(base, "themes", "light", "theme.json")
        if theme and theme.exists():
            with contextlib.suppress(OSError, ValueError):
                styles["__theme__"] = json.loads(theme.read_text(encoding="utf-8"))
        return styles

    def _style_class_files(self, project: str) -> dict[str, Path]:
        """Map style class names to their JSON files."""
        base = self.project_path(project) / PERSPECTIVE_MODULE
        found: dict[str, Path] = {}
        for folder in ("style-classes", "styles"):
            root = base / folder
            if not root.exists():
                continue
            for path in root.rglob("*.json"):
                if path.name == "resource.json":
                    continue
                found.setdefault(path.parent.name, path)
        return found

    # ------------------------------------------------------------------
    # Scripts
    # ------------------------------------------------------------------

    def list_scripts(self, project: str) -> list[str]:
        """List project library modules as dotted paths (``MyModule.SubModule``)."""
        root = self.project_path(project) / SCRIPT_PYTHON_DIR
        if not root.exists():
            return []
        return sorted(
            code_file.parent.relative_to(root).as_posix().replace("/", ".")
            for code_file in root.rglob("code.py")
        )

    def read_script(self, project: str, module_path: str) -> str | None:
        """Read a project library script's source code.

        Args:
            project: Project name.
            module_path: Dotted (``A.B``) or slashed (``A/B``) module path.

        Returns:
            The script source, or ``None`` when it does not exist.
        """
        script_dir = self.safe_path(
            self.project_path(project) / SCRIPT_PYTHON_DIR, module_path.replace(".", "/")
        )
        if not script_dir:
            return None
        code_file = script_dir / "code.py"
        if not code_file.exists():
            return None
        try:
            return code_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not read script %s: %s", module_path, exc)
            return None

    def write_script(self, project: str, module_path: str, code: str) -> Path | None:
        """Create or replace a project library script.

        Args:
            project: Project name.
            module_path: Dotted or slashed module path.
            code: Python/Jython source.

        Returns:
            The written ``code.py`` path, or ``None`` for an invalid path.
        """
        script_dir = self.safe_path(
            self.project_path(project) / SCRIPT_PYTHON_DIR, module_path.replace(".", "/")
        )
        if not script_dir:
            return None
        script_dir.mkdir(parents=True, exist_ok=True)
        code_file = script_dir / "code.py"
        if code_file.exists():
            snapshot_file(code_file, "write_script", project=project, module=module_path)
        code_file.write_text(code, encoding="utf-8")
        resource_file = script_dir / "resource.json"
        if not resource_file.exists():
            resource_file.write_text(
                json.dumps(
                    {
                        "scope": "G",
                        "version": 1,
                        "restricted": False,
                        "overridable": True,
                        "files": ["code.py"],
                        "config": {},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return code_file

    # ------------------------------------------------------------------
    # Named queries
    # ------------------------------------------------------------------

    def list_named_queries(self, project: str) -> list[str]:
        """List named query paths in a project."""
        root = self.project_path(project) / NAMED_QUERY_MODULE / NAMED_QUERY_DIR
        if not root.exists():
            return []
        return sorted(
            query_file.parent.relative_to(root).as_posix()
            for query_file in root.rglob("query.json")
        )

    def read_named_query(self, project: str, query_path: str) -> dict[str, Any] | None:
        """Read a named query's SQL and parameter definitions."""
        root = self.project_path(project) / NAMED_QUERY_MODULE / NAMED_QUERY_DIR
        query_file = self.safe_path(root, query_path, "query.json")
        if not query_file or not query_file.exists():
            return None
        try:
            return json.loads(query_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write_named_query(self, project: str, query_path: str, data: dict[str, Any]) -> Path | None:
        """Create or replace a named query resource.

        Args:
            project: Project name.
            query_path: Named query path, e.g. ``Production/GetCounts``.
            data: Decoded ``query.json`` payload.

        Returns:
            The written path, or ``None`` for an invalid path.
        """
        root = self.project_path(project) / NAMED_QUERY_MODULE / NAMED_QUERY_DIR
        query_file = self.safe_path(root, query_path, "query.json")
        if not query_file:
            return None
        if query_file.exists():
            snapshot_file(query_file, "write_named_query", project=project, query=query_path)
        query_file.parent.mkdir(parents=True, exist_ok=True)
        query_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return query_file

    def delete_named_query(self, project: str, query_path: str) -> Path | None:
        """Delete a named query resource after snapshotting it.

        Args:
            project: Project name.
            query_path: Named query path.

        Returns:
            The deleted path, or ``None`` when the path is invalid or missing.
        """
        root = self.project_path(project) / NAMED_QUERY_MODULE / NAMED_QUERY_DIR
        query_file = self.safe_path(root, query_path, "query.json")
        if not query_file or not query_file.exists():
            return None
        snapshot_file(query_file, "delete_named_query", project=project, query=query_path)
        query_file.unlink()
        parent = query_file.parent
        while parent != root and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return query_file

    # ------------------------------------------------------------------
    # Generic resources and search
    # ------------------------------------------------------------------

    def iter_project_files(
        self, project: str, suffixes: Sequence[str] = (".json",)
    ) -> Iterator[Path]:
        """Yield project files with the given suffixes."""
        base = self.project_path(project)
        if not base.exists():
            return
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in suffixes:
                yield path

    def list_resources(self, project: str) -> list[str]:
        """List every file in a project as a project-relative POSIX path."""
        base = self.project_path(project)
        if not base.exists():
            return []
        return sorted(
            path.relative_to(base).as_posix() for path in base.rglob("*") if path.is_file()
        )

    def read_resource(self, project: str, resource_path: str) -> Any | None:
        """Read any project resource, decoding ``.json`` files.

        Returns:
            Decoded JSON for ``.json`` resources, text otherwise, or ``None`` if not found.
        """
        path = self.safe_path(self.project_path(project), resource_path)
        if not path or not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if path.suffix.lower() == ".json":
            try:
                return json.loads(text)
            except ValueError:
                return text
        return text

    def write_resource(self, project: str, resource_path: str, data: Any) -> Path | None:
        """Write a project resource, JSON-encoding non-string payloads."""
        path = self.safe_path(self.project_path(project), resource_path)
        if not path:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            snapshot_file(path, "write_resource", project=project, resource=resource_path)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def search_text(
        self,
        project: str,
        needle: str,
        *,
        suffixes: Sequence[str] = (".json", ".py"),
        max_results: int = 200,
        projects: Iterable[str] | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
    ) -> list[dict[str, Any]]:
        """Search project resources for literal text or a regular expression.

        Args:
            project: Primary project to search.
            needle: Literal text or regex pattern to find.
            suffixes: File suffixes to include.
            max_results: Stop after this many matches.
            projects: Additional projects to search (for example inheritance parents).
            regex: Treat ``needle`` as a regular expression.
            case_sensitive: Match case exactly (default is case-insensitive).

        Returns:
            Matches as ``{"project", "resource", "line", "text"}`` dicts.
        """
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(needle if regex else re.escape(needle), flags)
        except re.error:
            return []
        matches: list[dict[str, Any]] = []
        for project_name in [project, *(projects or [])]:
            base = self.project_path(project_name)
            for path in self.iter_project_files(project_name, suffixes):
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for number, line in enumerate(lines, start=1):
                    if pattern.search(line):
                        matches.append(
                            {
                                "project": project_name,
                                "resource": path.relative_to(base).as_posix(),
                                "line": number,
                                "text": line.strip()[:300],
                            }
                        )
                        if len(matches) >= max_results:
                            return matches
        return matches

    def read_log_lines(self, n: int = 500) -> list[str]:
        """Return the last ``n`` lines of ``logs/wrapper.log``."""
        log_file = self.logs_dir / "wrapper.log"
        if not log_file.exists():
            return []
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-n:]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _normalise_key(key: str) -> str:
    """Lowercase and strip punctuation from a settings key."""
    return re.sub(r"[^a-z0-9.]", "", key.strip().lower())


def _first_int(settings: dict[str, str], keys: Sequence[str]) -> int | None:
    """Return the first parseable integer among ``keys``."""
    for key in keys:
        for candidate, value in settings.items():
            if key in candidate:
                match = re.search(r"\d{2,5}", value)
                if match:
                    return int(match.group(0))
    return None


def _first_bool(settings: dict[str, str], keys: Sequence[str]) -> bool:
    """Return the first boolean-like value among ``keys``."""
    for key in keys:
        for candidate, value in settings.items():
            if key in candidate:
                if value.strip().lower() in ("true", "1", "yes", "on"):
                    return True
                if value.strip().lower() in ("false", "0", "no", "off"):
                    return False
    return False


async def _distinct_names(db: Any, keywords: Sequence[str]) -> list[str]:
    """Return distinct values from the first name-like column of matching tables.

    Ignition's ``config.idb`` schema varies between versions, so tables and columns are
    discovered from ``sqlite_master`` instead of being hard-coded.

    Args:
        db: An open ``aiosqlite`` connection.
        keywords: Uppercase table-name fragments to match (e.g. ``("PROJECT",)``).

    Returns:
        Sorted, de-duplicated names.
    """
    try:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = [str(row[0]) for row in await cursor.fetchall()]
    except Exception:
        return []

    names: list[str] = []
    for table in tables:
        upper = table.upper()
        if not any(keyword in upper for keyword in keywords):
            continue
        quoted = table.replace('"', '""')
        try:
            async with db.execute(f'PRAGMA table_info("{quoted}")') as cursor:
                columns = [str(row[1]) for row in await cursor.fetchall()]
        except Exception:
            continue
        name_column = next((col for col in columns if col.upper().endswith("NAME")), None)
        if not name_column:
            continue
        quoted_column = name_column.replace('"', '""')
        try:
            async with db.execute(f'SELECT DISTINCT "{quoted_column}" FROM "{quoted}"') as cursor:
                rows = await cursor.fetchall()
        except Exception:
            continue
        names.extend(str(row[0]) for row in rows if row[0] not in (None, ""))
    return sorted(set(names))

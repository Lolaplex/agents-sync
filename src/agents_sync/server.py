"""Cloud vault server implementation for VPS/Coolify."""
from __future__ import annotations

import hmac
import os
import re
import threading
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

from .bundle import apply_bundle, collect_bundle
from .protocol import EP_HEALTH, EP_MERGE, EP_SNAPSHOT, EP_STATUS, PROTOCOL_VERSION
from .tombstones import load_tombstones, merge_tombstones, save_tombstones

_DEPRECATION = (
    "agents-sync Plane 1 (file-bundle HTTP sync) is deprecated; migrate to "
    "hub-channel op-sync (klanker serve wssync / agents_sync.opdist)."
)

_WORKSPACE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def resolve_server_token(secret_token: Optional[str] = None) -> str:
    return secret_token or os.environ.get("AGENTS_SYNC_TOKEN", "")


def get_server_vault_dir() -> Path:
    env_dir = os.environ.get("AGENTS_SYNC_VAULT_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / ".agents-sync-vault").resolve()


def create_app(
    vault_dir: Optional[Path] = None,
    secret_token: Optional[str] = None,
    allow_insecure_dev: bool = False,
):
    warnings.warn(_DEPRECATION, DeprecationWarning, stacklevel=2)

    expected_token = resolve_server_token(secret_token)
    if not expected_token and not allow_insecure_dev:
        raise SystemExit(
            "Refusing to start agents-sync server: AGENTS_SYNC_TOKEN is empty "
            "(fail-closed). Set a token or pass --allow-insecure-dev for "
            "local development only."
        )

    try:
        from fastapi import Body, Depends, FastAPI, HTTPException, Header, status
    except ImportError:
        raise RuntimeError("FastAPI is required to run the server. Install with: pip install 'agents-sync[server]'")

    root = (vault_dir or get_server_vault_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    merge_lock = threading.Lock()

    app = FastAPI(title="agents-sync-cloud", version=PROTOCOL_VERSION)

    def _workspace_root(workspace: Optional[str]) -> Path:
        """Vault partition for one workspace; slugs are locked down so a
        crafted value can never escape the vault root."""
        slug = (workspace or "").strip() or "default"
        if not _WORKSPACE_SLUG.match(slug):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid workspace identifier",
            )
        ws_root = (root / "workspaces" / slug).resolve()
        if not ws_root.is_relative_to(root):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid workspace identifier",
            )
        ws_root.mkdir(parents=True, exist_ok=True)
        return ws_root

    def _pick_workspace(
        workspace: Optional[str],
        header: Optional[str],
        payload: Optional[Dict[str, Any]] = None,
    ) -> Path:
        candidate = workspace or header
        if not candidate and payload:
            raw = payload.get("workspace")
            candidate = raw if isinstance(raw, str) else None
        return _workspace_root(candidate)

    def verify_auth(authorization: Optional[str] = Header(None)):
        if not expected_token:
            # Fail closed at request level too: an app started via
            # --allow-insecure-dev still refuses every authenticated route.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Server has no AGENTS_SYNC_TOKEN configured",
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")
        token = authorization.split("Bearer ", 1)[1].strip()
        if not hmac.compare_digest(token, expected_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")
        return True

    def _require_object(value: Any, field: str) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Malformed merge payload: {field} must be an object",
            )
        return value

    def _require_str_map(value: Dict[str, Any], field: str) -> Dict[str, str]:
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Malformed merge payload: {field} must map strings to strings",
                )
        return value

    @app.get(EP_HEALTH)
    def health_check():
        return {"status": "healthy", "version": PROTOCOL_VERSION}

    @app.get(EP_STATUS, dependencies=[Depends(verify_auth)])
    def server_status(
        workspace: Optional[str] = None,
        x_agents_workspace: Optional[str] = Header(None),
    ):
        ws_root = _pick_workspace(workspace, x_agents_workspace)
        stamps: Dict[str, str] = {}
        bundle = collect_bundle(ws_root, stamps=stamps)
        tombs = load_tombstones(ws_root)
        return {
            "status": "ok",
            "files_count": len(bundle),
            "tombstones_count": len(tombs.get("files", {})),
        }

    @app.get(EP_SNAPSHOT, dependencies=[Depends(verify_auth)])
    def get_snapshot(
        domains: Optional[str] = None,
        workspace: Optional[str] = None,
        x_agents_workspace: Optional[str] = Header(None),
    ):
        ws_root = _pick_workspace(workspace, x_agents_workspace)
        target_domains = [d.strip() for d in domains.split(",")] if domains else None
        stamps: Dict[str, str] = {}
        bundle = collect_bundle(ws_root, enabled_domains=target_domains, stamps=stamps)
        tombs = load_tombstones(ws_root)
        return {
            "bundle": bundle,
            "stamps": stamps,
            "tombstones": tombs,
        }

    @app.post(EP_MERGE, dependencies=[Depends(verify_auth)])
    def handle_merge(
        payload: Any = Body(...),
        workspace: Optional[str] = None,
        x_agents_workspace: Optional[str] = Header(None),
    ):
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed merge payload: top-level body must be an object",
            )
        ws_root = _pick_workspace(workspace, x_agents_workspace, payload)
        client_bundle = _require_str_map(
            _require_object(payload.get("bundle", {}), "bundle"), "bundle"
        )
        client_stamps = _require_str_map(
            _require_object(payload.get("stamps", {}), "stamps"), "stamps"
        )
        client_tombs = _require_object(
            payload.get("tombstones", {}), "tombstones"
        )
        for category in ("files", "prefixes", "bullets"):
            if not isinstance(client_tombs.get(category, {}), dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Malformed merge payload: "
                        f"tombstones.{category} must be an object"
                    ),
                )
        for category in ("files", "prefixes"):
            entries = client_tombs.get(category, {})
            if any(
                not isinstance(key, str) or not isinstance(stamp, str)
                for key, stamp in entries.items()
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Malformed merge payload: "
                        f"tombstones.{category} must map strings to strings"
                    ),
                )
        for file_key, bullet_map in client_tombs.get("bullets", {}).items():
            if not isinstance(file_key, str) or not isinstance(bullet_map, dict) or any(
                not isinstance(text, str) or not isinstance(stamp, str)
                for text, stamp in bullet_map.items()
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Malformed merge payload: tombstones.bullets must map "
                        "strings to objects of string timestamps"
                    ),
                )
        raw_domains = payload.get("domains")
        if raw_domains is None:
            target_domains = None
        elif isinstance(raw_domains, list) and all(
            isinstance(d, str) for d in raw_domains
        ):
            target_domains = raw_domains
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed merge payload: domains must be a list of strings",
            )

        # Serialized: concurrent merges on the same vault must not race.
        with merge_lock:
            apply_bundle(
                ws_root,
                client_bundle,
                remote_tombstones=client_tombs,
                remote_stamps=client_stamps,
                enabled_domains=target_domains,
            )

            # Merge server and client tombstones
            server_tombs = load_tombstones(ws_root)
            updated_tombs = merge_tombstones(server_tombs, client_tombs)
            save_tombstones(ws_root, updated_tombs)

            # Prepare merged response limited to the client's requested
            # domains — the server never forces all vault domains down.
            res_stamps: Dict[str, str] = {}
            res_bundle = collect_bundle(
                ws_root, enabled_domains=target_domains, stamps=res_stamps
            )

        return {
            "bundle": res_bundle,
            "stamps": res_stamps,
            "tombstones": updated_tombs,
        }

    return app


def run_server(
    port: int = 8765,
    host: str = "0.0.0.0",
    token: Optional[str] = None,
    allow_insecure_dev: bool = False,
):
    try:
        import uvicorn
    except ImportError:
        raise RuntimeError("uvicorn is required. Install with: pip install 'agents-sync[server]'")

    app = create_app(secret_token=token, allow_insecure_dev=allow_insecure_dev)
    uvicorn.run(app, host=host, port=port)

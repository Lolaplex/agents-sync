"""Cloud vault server implementation for VPS/Coolify."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from .bundle import apply_bundle, collect_bundle
from .domains import DOMAINS
from .protocol import EP_HEALTH, EP_MERGE, EP_SNAPSHOT, EP_STATUS, PROTOCOL_VERSION
from .tombstones import load_tombstones, merge_tombstones, save_tombstones


def resolve_server_token(secret_token: Optional[str] = None) -> str:
    return secret_token or os.environ.get("AGENTS_SYNC_TOKEN", "")


def get_server_vault_dir() -> Path:
    env_dir = os.environ.get("AGENTS_SYNC_VAULT_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / ".agents-sync-vault").resolve()


def create_app(vault_dir: Optional[Path] = None, secret_token: Optional[str] = None):
    try:
        from fastapi import Depends, FastAPI, HTTPException, Header, status
        from fastapi.responses import JSONResponse
    except ImportError:
        raise RuntimeError("FastAPI is required to run the server. Install with: pip install 'agents-sync[server]'")

    root = (vault_dir or get_server_vault_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    expected_token = resolve_server_token(secret_token)

    app = FastAPI(title="agents-sync-cloud", version=PROTOCOL_VERSION)

    def verify_auth(authorization: Optional[str] = Header(None)):
        if not expected_token:
            return True
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")
        token = authorization.split("Bearer ", 1)[1].strip()
        if token != expected_token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")
        return True

    @app.get(EP_HEALTH)
    def health_check():
        return {"status": "healthy", "version": PROTOCOL_VERSION}

    @app.get(EP_STATUS, dependencies=[Depends(verify_auth)])
    def server_status():
        stamps: Dict[str, str] = {}
        bundle = collect_bundle(root, stamps=stamps)
        tombs = load_tombstones(root)
        return {
            "status": "ok",
            "files_count": len(bundle),
            "tombstones_count": len(tombs.get("files", {})),
        }

    @app.get(EP_SNAPSHOT, dependencies=[Depends(verify_auth)])
    def get_snapshot(domains: Optional[str] = None):
        target_domains = [d.strip() for d in domains.split(",")] if domains else None
        stamps: Dict[str, str] = {}
        bundle = collect_bundle(root, enabled_domains=target_domains, stamps=stamps)
        tombs = load_tombstones(root)
        return {
            "bundle": bundle,
            "stamps": stamps,
            "tombstones": tombs,
        }

    @app.post(EP_MERGE, dependencies=[Depends(verify_auth)])
    def handle_merge(payload: Dict[str, Any]):
        client_bundle = payload.get("bundle", {})
        client_stamps = payload.get("stamps", {})
        client_tombs = payload.get("tombstones", {})

        # Apply client updates into server vault
        apply_bundle(
            root,
            client_bundle,
            remote_tombstones=client_tombs,
            remote_stamps=client_stamps,
        )

        # Merge server and client tombstones
        server_tombs = load_tombstones(root)
        updated_tombs = merge_tombstones(server_tombs, client_tombs)
        save_tombstones(root, updated_tombs)

        # Prepare full merged response for client
        res_stamps: Dict[str, str] = {}
        res_bundle = collect_bundle(root, stamps=res_stamps)

        return {
            "bundle": res_bundle,
            "stamps": res_stamps,
            "tombstones": updated_tombs,
        }

    return app


def run_server(port: int = 8765, host: str = "0.0.0.0", token: Optional[str] = None):
    try:
        import uvicorn
    except ImportError:
        raise RuntimeError("uvicorn is required. Install with: pip install 'agents-sync[server]'")

    app = create_app(secret_token=token)
    uvicorn.run(app, host=host, port=port)

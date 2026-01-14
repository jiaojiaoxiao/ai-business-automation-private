from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Depends, WebSocket, Form
from starlette.websockets import WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncpg
import os
import uuid
import asyncio
import time
import json
import logging
from typing import Dict, Any, Optional, List
from http import HTTPStatus
import importlib
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from .storage import get_storage
from .ocr_ai import analyze_document, _to_canonical_from_raw
from .db import init_db_pool, close_db_pool
from .repos import (
    resolve_by_id_or_code,
    exists_under_tenant,
    create_invoice,
    update_file_url,
    update_ocr_result,
    update_ai_result,
    update_project_id,
    mark_invoice_failed,
    mark_invoice_canceled,
)
from app.routes import classify

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")

# Feature flag: when false, do not call external Azure/OpenAI services or persist to DB.
USE_REAL = os.getenv("INGESTION_USE_REAL", "true").lower() in ("1", "true", "yes")

app = FastAPI(
    title="AI Business Automation - Ingestion Service",
    description="Document ingestion and OCR processing service",
    version="1.0.0"
)

app.include_router(classify.router)

# Structured logging helper
def log_structured(level: str, message: str, **fields: Any):
    payload = {"message": message}
    payload.update(fields)
    try:
        if level == 'info':
            logger.info(json.dumps(payload, ensure_ascii=False))
        elif level == 'warning':
            logger.warning(json.dumps(payload, ensure_ascii=False))
        elif level == 'error':
            logger.error(json.dumps(payload, ensure_ascii=False))
        else:
            logger.debug(json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.log(logging.INFO, message)

# Optional Prometheus metrics (enabled if prometheus_client available)
_metrics = None
try:
    prom = importlib.import_module('prometheus_client')
    from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
    registry = CollectorRegistry()
    METRIC_jobs_total = prom.Counter('ingestion_jobs_total', 'Total ingestion jobs', registry=registry)
    METRIC_jobs_failed = prom.Counter('ingestion_jobs_failed_total', 'Failed ingestion jobs', registry=registry)
    METRIC_job_duration = prom.Histogram('ingestion_job_duration_seconds', 'Job duration seconds', registry=registry)
    _metrics = True
except Exception:
    log_structured('warning', 'prometheus_client not available; metrics disabled')
    _metrics = False

# Configure CORS to allow browser preflight requests
_cors_origins = os.getenv("CORS_ALLOW_ORIGINS", "*")
if _cors_origins.strip() == "*":
    _allow_origins = ["*"]
else:
    _allow_origins = [o.strip() for o in _cors_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global WebSocket connections for progress updates
websocket_connections: Dict[str, WebSocket] = {}
# Buffer for events sent before a client registers for a job_id
websocket_event_buffers: Dict[str, List[Dict[str, Any]]] = {}
# Track running background tasks per invoice/job id so they can be canceled
job_tasks: Dict[str, asyncio.Task] = {}
job_last_active: Dict[str, float] = {}

# Background cleanup task handle
cleanup_task: Optional[asyncio.Task] = None

# Timeout configuration (seconds)
JOB_TIMEOUT_SECONDS = int(os.getenv('INGESTION_JOB_TIMEOUT_SECONDS', '600'))  # default 10 minutes
CLEANUP_INTERVAL_SECONDS = int(os.getenv('INGESTION_CLEANUP_INTERVAL_SECONDS', '30'))


async def _emit_analysis_complete(job_id: str, ocr_result: Dict[str, Any], ai_result: Dict[str, Any], filename: Optional[str] = None):
    """Helper to emit ANALYSIS_COMPLETE with minimal logging and proper buffering.

    Keeps a single place to control whether payloads are logged and how the event is sent/buffered.
    """
    try:
        payload = {'result': ocr_result, 'ai_result': ai_result, 'file_name': filename}
        # Do not log full payload to avoid noisy output in production; if needed, set debug flag elsewhere.
        await send_progress_event(job_id, 'ANALYSIS_COMPLETE', payload)
    except Exception:
        logger.exception("Failed to emit ANALYSIS_COMPLETE for job %s", job_id)


async def _run_stub_flow(tenant_id: str, invoice_id: str, file_bytes: bytes, filename: str, content_type: str, user_message: Optional[str] = None, test_index: Optional[int] = None):
    """Extracted stub-mode processing flow from `process_invoice_task`.

    Preserves existing behavior but centralizes the logic for clarity and easier testing.
    """
    try:
        await send_progress_event(invoice_id, 'DOC_RECEIVED')
        await asyncio.sleep(1)
        await send_progress_event(invoice_id, 'OCR_PROCESSING')
        # Prepare minimal, safe default stub results (keep types consistent)
        ocr_result = {
            "ocr_data": {},
            "ocr_content": "",
            "inferred_accounts": []
        }
        ai_result = {"inferred_accounts": [], "summary": ""}

        if test_index:
            try:
                base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
                test_path = os.path.join(base_dir, 'test-data', f'ai_result_{test_index}.json')
                if os.path.exists(test_path):
                    with open(test_path, 'r', encoding='utf-8') as tf:
                        loaded = json.load(tf)
                    ai_result = loaded
                ocr_test_path = os.path.join(base_dir, 'test-data', f'ocr_result_{test_index}.json')
                if os.path.exists(ocr_test_path):
                    try:
                        with open(ocr_test_path, 'r', encoding='utf-8') as of:
                            loaded_ocr = json.load(of)
                        ocr_result = loaded_ocr
                        if 'file_name' in loaded_ocr and 'file_name' not in ai_result:
                            ai_result['file_name'] = loaded_ocr.get('file_name')
                    except Exception:
                        logger.exception("Failed to parse ocr_result test file %s", ocr_test_path)
                        ocr_result['inferred_accounts'] = ai_result.get('inferred_accounts', [])
                else:
                    ocr_result['inferred_accounts'] = ai_result.get('inferred_accounts', [])
                    if 'file_name' in ai_result:
                        ocr_result['file_name'] = ai_result.get('file_name')
                    else:
                        ocr_result['file_name'] = filename
            except Exception:
                logger.exception("Failed to load test-data ai_result for index %s", test_index)
                ocr_result['file_name'] = filename
                ai_result['file_name'] = filename
        else:
            ocr_result['file_name'] = filename
            ai_result['file_name'] = filename

        await asyncio.sleep(1)
        await send_progress_event(invoice_id, 'AI_THINKING')
        await asyncio.sleep(0.5)
        try:
            try:
                ocr_result_canonical = _to_canonical_from_raw(file_name=ocr_result.get('file_name'), ocr_data=ocr_result.get('ocr_data'), ocr_content=ocr_result.get('ocr_content'), inferred_accounts_raw=ocr_result.get('inferred_accounts'))
                ai_result_canonical = _to_canonical_from_raw(file_name=ai_result.get('file_name'), ocr_data=None, ocr_content=None, inferred_accounts_raw=ai_result.get('inferred_accounts'))
            except Exception:
                ocr_result_canonical = ocr_result
                ai_result_canonical = ai_result
        except Exception:
            logger.exception("Failed to prepare ANALYSIS_COMPLETE payload for job %s", invoice_id)

        await _emit_analysis_complete(invoice_id, ocr_result_canonical, ai_result_canonical, filename)
    except asyncio.CancelledError:
        await send_progress_event(invoice_id, 'CANCELED', {'reason': 'task_cancelled'})
        raise
    except Exception as e:
        logger.error(f"Unexpected error in simulated process_invoice_task for {invoice_id}: {e}")
    finally:
        job_tasks.pop(invoice_id, None)
        job_last_active.pop(invoice_id, None)


# --- Response models for /chat API ---
class InvoiceData(BaseModel):
    invoice_id: str
    file_url: Optional[str] = None
    ocr_result: Optional[Dict[str, Any]] = None
    ai_result: Optional[Dict[str, Any]] = None


class Message(BaseModel):
    id: str
    role: str
    type: str
    content: str
    invoiceData: Optional[InvoiceData] = None
    suggestions: List[Any] = Field(default_factory=list)


class ChatResponse(BaseModel):
    jobId: str
    status: str
    messages: List[Message] = Field(default_factory=list)
    # Optional top-level single-line message (for ack/init style)
    message: Optional[str] = None
    suggestions: List[Any] = Field(default_factory=list)
    invoiceIds: Optional[List[str]] = None


class ChatInitResponse(BaseModel):
    message: str
    suggestions: List[Any] = Field(default_factory=list)


class CancelResponse(BaseModel):
    jobId: str
    status: str


async def send_progress_event(job_id: str, event: str, data: Dict[str, Any] = None):
    """
    Send progress event to WebSocket client, or buffer it if client not connected.
    Also update last-activity timestamp for timeout tracking.
    """
    message = {"event": event, "job_id": job_id}
    if data:
        message.update(data)

    # update last active timestamp whenever an event is emitted
    try:
        job_last_active[job_id] = time.time()
    except Exception:
        pass

    if job_id in websocket_connections:
        try:
            await websocket_connections[job_id].send_json(message)
            logger.info(f"Sent progress event {event} for job {job_id}")
            return
        except Exception:
            # On failure, log full traceback and ensure message is buffered for retry
            logger.exception("Failed to send WebSocket event %s for job %s, buffering it", event, job_id)
            try:
                websocket_event_buffers.setdefault(job_id, []).append(message)
                logger.info(f"Buffered progress event {event} for job {job_id} after send failure (buffer_size={len(websocket_event_buffers[job_id])})")
            except Exception:
                logger.exception("Failed to buffer WebSocket event after send failure for job %s", job_id)
            return

    # Buffer if no client connected
    try:
        websocket_event_buffers.setdefault(job_id, []).append(message)
        logger.info(f"Buffered progress event {event} for job {job_id} (buffer_size={len(websocket_event_buffers[job_id])})")
    except Exception:
        logger.exception("Failed to buffer WebSocket event for job %s", job_id)


async def cleanup_stale_jobs_loop():
    """Periodically cancel and cleanup jobs that have been idle beyond timeout."""
    logger.info("Starting cleanup_stale_jobs_loop")
    try:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            now = time.time()
            stale = []
            for job_id in list(job_tasks.keys()):
                last = job_last_active.get(job_id)
                if last is None:
                    # no events emitted yet; skip this check (give grace)
                    continue
                if now - last > JOB_TIMEOUT_SECONDS:
                    stale.append(job_id)

            for job_id in stale:
                logger.warning(f"Job {job_id} timed out after {JOB_TIMEOUT_SECONDS}s, cancelling")
                task = job_tasks.get(job_id)
                try:
                    if task and not task.done():
                        task.cancel()
                except Exception as e:
                    logger.error(f"Failed to cancel task for stale job {job_id}: {e}")

                # mark invoice failed due to timeout
                try:
                    if USE_REAL:
                        conn = await app.state.db_pool.acquire()
                        try:
                            await mark_invoice_failed(conn, job_id, 'timeout')
                        finally:
                            await app.state.db_pool.release(conn)
                except Exception as e:
                    logger.error(f"Failed to mark invoice failed for timed out job {job_id}: {e}")

                try:
                    await send_progress_event(job_id, 'TIMED_OUT', {'reason': 'timeout'})
                except Exception as e:
                    logger.error(f"Failed to send TIMED_OUT event for {job_id}: {e}")

                # cleanup buffers and connections
                try:
                    websocket_event_buffers.pop(job_id, None)
                except Exception:
                    pass
                try:
                    if job_id in websocket_connections:
                        try:
                            await websocket_connections[job_id].close()
                        except Exception:
                            pass
                        del websocket_connections[job_id]
                except Exception:
                    pass

                job_tasks.pop(job_id, None)
                job_last_active.pop(job_id, None)
    except asyncio.CancelledError:
        logger.info('cleanup_stale_jobs_loop cancelled')
    except Exception as e:
        log_structured('error', 'cleanup_stale_jobs_loop crashed', error=str(e))


async def process_invoice_task(tenant_id: str, invoice_id: str, file_bytes: bytes, filename: str, content_type: str, user_message: Optional[str] = None, file_url: Optional[str] = None, test_index: Optional[int] = None):
    """Background task to process an invoice: perform (stubbed) OCR/AI, persist results, and emit progress events."""
    # If USE_REAL is False, run a simulated processing flow that emits the same events
    # but does not perform any DB writes or call external services.
    if not USE_REAL:
        await _run_stub_flow(tenant_id, invoice_id, file_bytes, filename, content_type, user_message, test_index)
        return

    # Real flow (uses DB and external services)
    conn = None
    try:
        conn = await app.state.db_pool.acquire()
        # initial event
        await send_progress_event(invoice_id, 'DOC_RECEIVED')

        # Ensure RLS session variable is set for background connection
        try:
            await conn.execute(f"SET SESSION app.current_tenant_id = '{tenant_id}'")
        except Exception as e:
            logger.warning(f"Failed to set RLS session for tenant {tenant_id}: {e}")

        # Determine file URL: prefer provided file_url (saved earlier), otherwise save now
        file_to_analyze = file_url
        if not file_to_analyze:
            try:
                filename_safe = filename.replace(' ', '_')
                storage_key = f"{tenant_id}/{invoice_id}_{filename_safe}"
                file_to_analyze = await app.state.storage.save(storage_key, file_bytes)
                # persist file_url
                try:
                    await update_file_url(conn, invoice_id, file_to_analyze)
                except Exception:
                    logger.warning(f"Failed to update file_url for {invoice_id}")
            except Exception as e:
                logger.error(f"Failed to save file for analysis for {invoice_id}: {e}")
                await mark_invoice_failed(conn, invoice_id, 'storage_save_failed')
                return

        # Call real analyzer (may raise) and persist results
        try:
            await send_progress_event(invoice_id, 'OCR_PROCESSING')
            analysis = await analyze_document(file_to_analyze, tenant_id=tenant_id, job_id=invoice_id, progress_callback=send_progress_event, user_message=user_message)

            # Prefer canonicalized results from analyzer when available
            ocr_result = analysis.get('ocr_result') if analysis.get('ocr_result') else {
                "ocr_data": analysis.get('ocr_data', {}),
                "ocr_content": analysis.get('ocr_content', ''),
                "inferred_accounts": analysis.get('inferred_accounts', [])
            }
            # propagate filename into results so UI can show it
            try:
                ocr_result["file_name"] = filename
            except Exception:
                pass
            try:
                await update_ocr_result(conn, invoice_id, ocr_result)
            except Exception as e:
                logger.error(f"Failed to persist OCR result for {invoice_id}: {e}")

            await send_progress_event(invoice_id, 'AI_THINKING')
            ai_result = analysis.get('ai_result') if analysis.get('ai_result') else {"inferred_accounts": ocr_result.get('inferred_accounts', []), "summary": analysis.get('summary', '')}
            try:
                ai_result["file_name"] = filename
            except Exception:
                pass
            try:
                await update_ai_result(conn, invoice_id, ai_result)
            except Exception as e:
                logger.error(f"Failed to persist AI result for {invoice_id}: {e}")

            try:
                # payload prepared; omit logging of full payload to reduce noisy output
                pass
            except Exception:
                logger.exception("Failed to prepare ANALYSIS_COMPLETE payload for job %s", invoice_id)
            await send_progress_event(invoice_id, 'ANALYSIS_COMPLETE', {'result': ocr_result, 'ai_result': ai_result, 'file_name': filename})

        except Exception as e:
            logger.error(f"Analysis failed for {invoice_id}: {e}")
            try:
                await mark_invoice_failed(conn, invoice_id, str(e))
            except Exception:
                pass

    except asyncio.CancelledError:
        logger.info(f"process_invoice_task cancelled for {invoice_id}")
        try:
            if conn:
                await mark_invoice_canceled(conn, invoice_id, 'task_cancelled')
        except Exception as e:
            logger.error(f"Failed to mark canceled for {invoice_id}: {e}")
        await send_progress_event(invoice_id, 'CANCELED', {'reason': 'task_cancelled'})
        raise
    except Exception as e:
        logger.error(f"Unexpected error in process_invoice_task for {invoice_id}: {e}")
        try:
            if conn:
                await mark_invoice_failed(conn, invoice_id, str(e))
        except Exception:
            pass
    finally:
        if conn:
            try:
                await app.state.db_pool.release(conn)
            except Exception:
                pass
        job_tasks.pop(invoice_id, None)
        job_last_active.pop(invoice_id, None)


async def get_tenant_id_from_header(request: Request) -> str:
    """
    Extract and validate tenant ID from request headers.

    Args:
        request: FastAPI request object.

    Returns:
        Validated tenant UUID string.

    Raises:
        HTTPException: If tenant ID is missing or invalid.
    """
    tenant_id = request.headers.get("X-Tenant-ID")
    if not tenant_id:
        logger.warning("Missing X-Tenant-ID header")
        raise HTTPException(status_code=400, detail="X-Tenant-ID header is required")

    try:
        # Validate UUID format
        uuid.UUID(tenant_id)
        return tenant_id
    except ValueError:
        logger.warning(f"Invalid tenant ID format: {tenant_id}")
        raise HTTPException(status_code=400, detail="X-Tenant-ID must be a valid UUID")


@app.get('/health')
async def health():
    return JSONResponse(content={"status": "ok"}, status_code=HTTPStatus.OK)


@app.get('/ready')
async def readiness():
    """Readiness probe: checks DB pool and storage initialization. Does not attempt LLM warmup by default."""
    checks = {"db": False, "storage": False}
    try:
        if hasattr(app.state, 'db_pool') and app.state.db_pool is not None:
            # try acquire/release a connection
            try:
                conn = await app.state.db_pool.acquire()
                await app.state.db_pool.release(conn)
                checks['db'] = True
            except Exception:
                checks['db'] = False
        if hasattr(app.state, 'storage') and app.state.storage is not None:
            checks['storage'] = True
    except Exception as e:
        log_structured('error', 'Readiness check failed', error=str(e))

    status = HTTPStatus.OK if all(checks.values()) else HTTPStatus.SERVICE_UNAVAILABLE
    return JSONResponse(content={"ready": all(checks.values()), "checks": checks}, status_code=status)


@app.on_event("startup")
async def startup():
    """Initialize database connection pool and storage on startup."""
    try:
        # Optional fail-fast credentials check controlled by env var
        require_credentials = os.getenv('REQUIRE_CREDENTIALS', 'false').lower() in ('1', 'true', 'yes')
        if require_credentials:
            missing = []
            # check core credentials
            for k in ('AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT', 'AZURE_DOCUMENT_INTELLIGENCE_KEY'):
                if not os.getenv(k):
                    missing.append(k)
            if not (os.getenv('OPENAI_API_KEY') or (os.getenv('AZURE_OPENAI_ENDPOINT') and os.getenv('AZURE_OPENAI_API_KEY'))):
                missing.append('OPENAI/AZURE_OPENAI')
            if missing:
                raise RuntimeError(f"Missing required credentials: {missing}")

        await init_db_pool(app, DATABASE_URL)
        log_structured('info', 'Database connection pool created successfully')

        app.state.storage = get_storage()
        log_structured('info', 'Storage backend initialized successfully')
        # start cleanup background loop
        global cleanup_task
        try:
            cleanup_task = asyncio.create_task(cleanup_stale_jobs_loop())
        except Exception as e:
            logger.error(f"Failed to start cleanup task: {e}")

    except Exception as e:
        log_structured('error', 'Startup failed', error=str(e))
        raise


@app.on_event("shutdown")
async def shutdown():
    """Clean up resources on shutdown."""
    try:
        # cancel cleanup task
        global cleanup_task
        if cleanup_task:
            try:
                cleanup_task.cancel()
                await cleanup_task
            except Exception:
                pass
        await close_db_pool(app)
        logger.info("Database connection pool closed")

    except Exception as e:
        log_structured('error', 'Shutdown error', error=str(e))


@app.middleware("http")
async def tenant_middleware(request: Request, call_next):
    """
    Middleware to handle tenant isolation and database connections.

    Sets up tenant-specific database session and connection for each request.
    """
    # (removed skip) always enforce tenant/user validation for all endpoints

    # Allow CORS preflight requests through without tenant/user checks
    if request.method == "OPTIONS":
        return await call_next(request)

    try:
        # Read raw tenant header and log for debugging
        raw_tenant = request.headers.get("X-Tenant-ID")
        # Ensure we always log tenant header at INFO so it's visible in typical logging
        logger.info(f"Incoming X-Tenant-ID header: {raw_tenant}")

        if not raw_tenant:
            logger.warning("Missing X-Tenant-ID header")
            raise HTTPException(status_code=400, detail="X-Tenant-ID header is required")

        # Normalize header (trim whitespace and surrounding quotes)
        raw_tenant = raw_tenant.strip()
        if (raw_tenant.startswith('"') and raw_tenant.endswith('"')) or (raw_tenant.startswith("'") and raw_tenant.endswith("'")):
            raw_tenant = raw_tenant[1:-1]

        # Acquire database connection early so we can fallback to tenant code lookup
        conn = await app.state.db_pool.acquire()
        request.state.db_conn = conn

        # Determine tenant_id: accept either UUID or tenant code using tenant repo
        tenant_id = await resolve_by_id_or_code(conn, raw_tenant)
        if tenant_id:
            logger.info(f"Resolved tenant identifier '{raw_tenant}' to id {tenant_id}")

        if not tenant_id:
            logger.warning(f"Invalid tenant identifier provided: {raw_tenant}")
            raise HTTPException(status_code=400, detail="X-Tenant-ID must be a valid UUID or existing tenant code")

        request.state.tenant_id = tenant_id

        # Set RLS session variable
        await conn.execute(f"SET SESSION app.current_tenant_id = '{tenant_id}'")

        # Validate user header exists and that user belongs to tenant
        user_id = request.headers.get("X-USER-ID")
        if not user_id:
            logger.warning("Missing X-USER-ID header")
            raise HTTPException(status_code=400, detail="X-USER-ID header is required")

        # Verify user exists under tenant via repo
        try:
            exists = await exists_under_tenant(conn, user_id, tenant_id)
        except Exception as db_err:
            logger.error(f"DB error when validating user: {db_err}")
            raise HTTPException(status_code=500, detail="User validation failed")

        if not exists:
            logger.warning(f"User {user_id} not found under tenant {tenant_id}")
            raise HTTPException(status_code=401, detail="User not found under tenant")

        request.state.user_id = user_id

        # Process request
        response = await call_next(request)
        return response

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Middleware error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        # Always release connection
        if hasattr(request.state, 'db_conn'):
            try:
                await app.state.db_pool.release(request.state.db_conn)
            except Exception as e:
                logger.error(f"Error releasing database connection: {e}")

@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket):
    """
    WebSocket endpoint for real-time progress updates during document processing.
    """
    await websocket.accept()
    job_id = None
    try:
        # Receive initial job_id from client
        data = await websocket.receive_json()
        job_id = data.get("job_id")
        if job_id:
            websocket_connections[job_id] = websocket
            log_structured('info', 'WebSocket registered', job_id=job_id)
            # flush any buffered events for this job_id
            if job_id in websocket_event_buffers:
                buffered = websocket_event_buffers[job_id]
                logger.info(f"Attempting to flush {len(buffered)} buffered events for job {job_id}")
                remaining = []
                for i, ev in enumerate(buffered):
                    try:
                        await websocket.send_json(ev)
                    except Exception:
                        logger.exception("Failed to send buffered event index %s for job %s", i, job_id)
                        remaining = buffered[i:]
                        break

                if remaining:
                    websocket_event_buffers[job_id] = remaining
                    log_structured('warning', 'Left buffered events after send failure', job_id=job_id, remaining=len(remaining))
                else:
                    try:
                        del websocket_event_buffers[job_id]
                    except KeyError:
                        pass
                    log_structured('info', 'Flushed all buffered events', job_id=job_id)

            # After flush attempt, try to query DB for persisted results and send them
            try:
                conn = await app.state.db_pool.acquire()
                try:
                    row = await conn.fetchrow('SELECT ocr_result, ai_result FROM "Invoice" WHERE id = $1', job_id)
                    if row:
                        ocr_res = row.get('ocr_result')
                        ai_res = row.get('ai_result')
                        if ai_res or ocr_res:
                            payload = {"event": "ANALYSIS_COMPLETE", "job_id": job_id, "result": ocr_res, "ai_result": ai_res}
                            try:
                                await websocket.send_json(payload)
                                log_structured('info', 'Delivered persisted ANALYSIS_COMPLETE', job_id=job_id)
                            except Exception:
                                logger.exception("Failed to deliver persisted ANALYSIS_COMPLETE for job %s", job_id)
                finally:
                    await app.state.db_pool.release(conn)
            except Exception:
                logger.exception("DB lookup for persisted results failed for job %s", job_id)

        while True:
            # Keep connection alive
            await asyncio.sleep(30)
            try:
                await websocket.send_json({"type": "ping"})
            except WebSocketDisconnect:
                # client disconnected cleanly; break loop to run cleanup
                logger.info("WebSocket (path) client disconnected during ping for job %s", job_id)
                break
            except Exception:
                # log and break on other send errors to allow cleanup
                logger.exception("Unexpected error sending ping on WebSocket (path) for job %s", job_id)
                break

    except Exception as e:
        logger.exception("WebSocket error (accept/register) for job %s", job_id)
    finally:
        if job_id and job_id in websocket_connections:
            del websocket_connections[job_id]
        logger.info(f"WebSocket connection closed for job {job_id}")


@app.websocket("/ws/progress/{job_id}")
async def websocket_progress_by_path(websocket: WebSocket, job_id: str):
    """WebSocket endpoint that accepts job_id in the URL path.

    This provides a simpler client implementation (no initial JSON registration).
    """
    await websocket.accept()
    try:
        if job_id:
            websocket_connections[job_id] = websocket
            logger.info(f"WebSocket (path) registered for job {job_id}")
            # flush buffered events
            if job_id in websocket_event_buffers:
                buffered = websocket_event_buffers[job_id]
                logger.info(f"Attempting to flush {len(buffered)} buffered events for job {job_id} (path)")
                remaining = []
                for i, ev in enumerate(buffered):
                    try:
                        await websocket.send_json(ev)
                    except Exception:
                        logger.exception("Failed to send buffered event index %s for job %s (path)", i, job_id)
                        remaining = buffered[i:]
                        break

                if remaining:
                    websocket_event_buffers[job_id] = remaining
                    logger.warning(f"Left {len(remaining)} buffered events for job {job_id} (path) after send failure")
                else:
                    try:
                        del websocket_event_buffers[job_id]
                    except KeyError:
                        pass
                    logger.info(f"Flushed all buffered events for job {job_id} (path)")

            # DB fallback: if invoice results already persisted, send ANALYSIS_COMPLETE immediately
            try:
                conn = await app.state.db_pool.acquire()
                try:
                    row = await conn.fetchrow('SELECT ocr_result, ai_result FROM "Invoice" WHERE id = $1', job_id)
                    if row:
                        ocr_res = row.get('ocr_result')
                        ai_res = row.get('ai_result')
                        if ai_res or ocr_res:
                            payload = {"event": "ANALYSIS_COMPLETE", "job_id": job_id, "result": ocr_res, "ai_result": ai_res}
                            try:
                                await websocket.send_json(payload)
                                log_structured('info', 'Delivered persisted ANALYSIS_COMPLETE', job_id=job_id)
                            except Exception:
                                logger.exception("Failed to deliver persisted ANALYSIS_COMPLETE for job %s (path)", job_id)
                finally:
                    await app.state.db_pool.release(conn)
            except Exception:
                logger.exception("DB lookup for persisted results failed for job %s (path)", job_id)

        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping"})

    except Exception as e:
        logger.exception("WebSocket (path) error for job %s", job_id)
    finally:
        if job_id and job_id in websocket_connections:
            try:
                del websocket_connections[job_id]
            except Exception:
                pass
        logger.info(f"WebSocket (path) connection closed for job {job_id}")

@app.post("/chat", response_model=ChatResponse)
async def chat_with_files(
    request: Request,
    files: Optional[List[UploadFile]] = File(None),
    message: Optional[str] = Form(None)
) -> ChatResponse:
    """
    Unified chat endpoint: accepts files and/or a message.

    - If files are provided: store them, create invoice records, run OCR + AI (passing user message), and return per-file results.
    - If only message is provided: call AI inference directly.
    """
    job_id = str(uuid.uuid4())
    tenant_id = getattr(request.state, 'tenant_id', None)
    user_id = getattr(request.state, 'user_id', None)
    conn = getattr(request.state, 'db_conn', None)

    logger.info(f"Starting chat job {job_id} for tenant {tenant_id}, user {user_id}")

    # Log incoming payload summary for debugging: message + files count
    try:
        incoming_msg = message if isinstance(message, str) else str(message)
        logger.info(f"/chat received: message (truncated 200): {incoming_msg[:200]}, files_count: {len(files) if files else 0}")
    except Exception:
        logger.warning("Failed to log incoming /chat payload summary")

    if not tenant_id or not conn:
        logger.error("Missing tenant or DB connection in request state")
        raise HTTPException(status_code=500, detail="Server configuration error")

    results = []
    try:
        # If files provided, process each file: create invoice, store, OCR+AI
        if files:
            file_index = 0
            for file in files:
                file_index += 1
                if not file.filename:
                    continue

                allowed_types = ['application/pdf', 'image/jpeg', 'image/png', 'image/jpg']
                if file.content_type not in allowed_types:
                    logger.warning(f"Unsupported file type: {file.content_type} for file {file.filename}")
                    continue

                # If USE_REAL is enabled, create a DB invoice record; otherwise generate a transient id
                if USE_REAL:
                    invoice_id = await create_invoice(conn, tenant_id)
                    logger.info(f"Created invoice record: {invoice_id}")
                else:
                    invoice_id = str(uuid.uuid4())
                    logger.info(f"Generated transient invoice id (stub mode): {invoice_id}")

                # Read content and validate
                content = await file.read()
                if len(content) == 0:
                    logger.warning(f"Empty file: {file.filename}")
                    await mark_invoice_failed(conn, invoice_id, "Empty file")
                    continue

                # Create file_url by saving to configured storage when in real mode,
                # otherwise use a simulated local path.
                filename_safe = file.filename.replace(' ', '_')
                storage_key = f"{tenant_id}/{invoice_id}_{filename_safe}"
                if USE_REAL:
                    try:
                        saved_url = await app.state.storage.save(storage_key, content)
                        file_url = saved_url
                        await update_file_url(conn, invoice_id, file_url)
                    except Exception as e:
                        logger.error(f"Failed to save file for invoice {invoice_id}: {e}")
                        await mark_invoice_failed(conn, invoice_id, "storage_save_failed")
                        continue
                else:
                    file_url = f"/tmp/ingestion/{tenant_id}/{invoice_id}_{filename_safe}"

                # schedule background processing task (cancelable)
                # Log message being passed to background task (sanitized/truncated)
                try:
                    if message:
                        mlog = str(message).replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'")
                        logger.info(f"Scheduling background task for invoice {invoice_id} with user_message (truncated 200): {mlog[:200]}")
                    else:
                        logger.info(f"Scheduling background task for invoice {invoice_id} with no user_message")
                except Exception:
                    logger.warning(f"Failed to sanitize/log message for invoice {invoice_id}")

                # pass test_index when in stub mode so process_invoice_task can load test-data
                if USE_REAL:
                    task = asyncio.create_task(process_invoice_task(tenant_id, invoice_id, content, file.filename, file.content_type, message, file_url=file_url))
                else:
                    task = asyncio.create_task(process_invoice_task(tenant_id, invoice_id, content, file.filename, file.content_type, message, file_url=file_url, test_index=file_index))
                job_tasks[invoice_id] = task
                job_last_active[invoice_id] = time.time()

                # optimistic response: include invoice id and file_url; final OCR/AI will be sent via WS
                results.append({
                    "invoice_id": invoice_id,
                    "file_url": file_url,
                    "file_name": file.filename,
                    "ocr_result": None
                })

            # Return a single acknowledgement message. Actual invoice cards
            # are produced on the frontend only when a WS 'ANALYSIS_COMPLETE'
            # event arrives (per design doc). This avoids duplicate/optimistic
            # card creation on initial POST.
            ack_message = {
                "id": job_id,
                "role": "ai",
                "type": "text",
                "content": f"受け取ったよ！Job IDは {job_id} です",
                "invoiceData": None,
                "suggestions": []
            }
            invoice_ids = [r.get('invoice_id') for r in results if r.get('invoice_id')]
            log_structured('info', 'Acknowledged job', job_id=job_id, invoice_ids=invoice_ids)

            return {"jobId": job_id, "status": "success", "messages": [ack_message], "message": ack_message.get('content'), "suggestions": ack_message.get('suggestions', []), "invoiceIds": invoice_ids}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Chat processing failed: {e}")
        # attempt to mark last invoice as failed if exists
        if conn and 'invoice_id' in locals():
            try:
                await mark_invoice_failed(conn, invoice_id, str(e))
            except Exception as db_error:
                logger.error(f"Failed to update invoice status: {db_error}")

        raise HTTPException(status_code=500, detail=f"Chat processing failed: {str(e)}")


@app.get("/chat/init", response_model=ChatInitResponse)
async def chat_init():
    """
    Return initial welcome message and suggestions for first-time chat.

    For now, load a fixed JSON from config/initial_chat.json. In future,
    this can be tenant-specific and loaded from DB.
    """
    try:
        base_dir = os.path.dirname(__file__)
        cfg_path = os.path.join(base_dir, "config", "initial_chat.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return JSONResponse(content=data)

        # fallback default
        data = {
            "message": "こんにちは！BWC会計支援AIです。請求書をアップロードしてください。プロジェクトの指定や科目の補足があればメッセージで教えてください。",
            "suggestions": []
        }
        return JSONResponse(content=data)
    except Exception as e:
        logger.error(f"Failed to load initial chat config: {e}")
        raise HTTPException(status_code=500, detail="Failed to load initial chat message")


@app.post("/chat/{job_id}/cancel", response_model=CancelResponse)
async def cancel_chat_job(job_id: str, request: Request) -> CancelResponse:
    """Cancel a running chat/job (invoice) by ID.

    This will mark the invoice as canceled in the database and emit a
    WebSocket progress event so any connected clients can react.
    """
    tenant_id = getattr(request.state, 'tenant_id', None)
    conn = getattr(request.state, 'db_conn', None)

    if not tenant_id or not conn:
        logger.error("Missing tenant or DB connection in cancel request")
        raise HTTPException(status_code=500, detail="Server configuration error")

    try:
        # mark invoice canceled in DB (catch DB errors so cancel API doesn't 500)
        if USE_REAL:
            try:
                await mark_invoice_canceled(conn, job_id, f"Canceled by user request")
            except Exception:
                logger.exception("Failed to mark invoice canceled in DB for job %s", job_id)

        # Cancel running background task if present
        if job_id in job_tasks:
            task = job_tasks[job_id]
            try:
                task.cancel()
            except Exception as e:
                logger.warning(f"Failed to cancel task for {job_id}: {e}")

        # notify websocket clients (if any) — do not let WS errors break cancel API
        try:
            await send_progress_event(job_id, "CANCELED", {"reason": "canceled_by_user"})
        except Exception:
            logger.exception("Failed to send CANCELED progress event for job %s", job_id)

        logger.info(f"Job {job_id} canceled by user")
        return CancelResponse(jobId=job_id, status="canceled")
    except Exception as e:
        logger.error(f"Failed to cancel job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
import json
import logging
import os
import socket
import ast
import time
import unicodedata
from urllib import error, request
 
import psycopg
import whisper
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("finance_api")


MODEL_NAME = os.getenv("WHISPER_MODEL", "base")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "140"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "1024"))
OLLAMA_NUM_THREAD = int(os.getenv("OLLAMA_NUM_THREAD", "4"))
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
OLLAMA_DEBUG_STREAM = os.getenv("OLLAMA_DEBUG_STREAM", "false").lower() == "true"
DEBUG_PAYLOADS = os.getenv("DEBUG_PAYLOADS", "false").lower() == "true"
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".webm", ".flac"}
INCOME_CATEGORIES = {
    "salario",
    "freelance",
    "investimentos",
    "vendas",
    "reembolso",
    "bonus",
    "outros_receitas",
}
EXPENSE_CATEGORIES = {
    "alimentacao",
    "moradia",
    "transporte",
    "saude",
    "educacao",
    "lazer",
    "impostos",
    "assinaturas",
    "contas",
    "compras",
    "outros_gastos",
}

app = FastAPI(title="Finance Whisper Agent API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_whisper_model = None


def get_whisper_model():
    """Carrega Whisper sob demanda para nao competir por RAM com Ollama em /api/agent/execute."""
    global _whisper_model
    if _whisper_model is None:
        logger.info("Carregando Whisper (primeira transcricao)...", extra={"whisper_model": MODEL_NAME})
        _whisper_model = whisper.load_model(MODEL_NAME)
    return _whisper_model


class FinanceCreate(BaseModel):
    amount: float = Field(..., gt=0)
    category: str = Field(..., min_length=1)
    description: str = ""
    occurred_on: str | None = None
    phone: str | None = None


class AgentExecuteRequest(BaseModel):
    text: str = Field(..., min_length=1)
    confirm: bool = False
    model: str | None = None
    phone: str | None = None


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        logger.error("DATABASE_URL nao configurada.")
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL nao configurada no ambiente.",
        )
    return url


def get_conn() -> psycopg.Connection:
    return psycopg.connect(get_database_url(), row_factory=dict_row)


def init_db() -> None:
    logger.info("Inicializando estrutura de banco...")
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS user_phone TEXT")
        conn.execute("UPDATE transactions SET user_phone = 'anonimo' WHERE user_phone IS NULL")
        conn.execute("ALTER TABLE transactions ALTER COLUMN user_phone SET NOT NULL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_user_phone ON transactions(user_phone)"
        )
    logger.info("Banco inicializado com sucesso.")


def normalize_phone(phone: str | None) -> str:
    if not phone:
        return "anonimo"
    digits = "".join(char for char in phone if char.isdigit())
    return digits or "anonimo"


def ensure_user(phone: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (phone)
            VALUES (%s)
            ON CONFLICT (phone) DO NOTHING
            """,
            [phone],
        )


def normalize_date(occurred_on: str | None) -> str:
    if not occurred_on:
        return date.today().isoformat()
    try:
        return datetime.strptime(occurred_on, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Data invalida. Use YYYY-MM-DD.") from exc


def normalize_category(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    # Normalizacoes de singular/plural comuns retornados por LLMs.
    # Ex.: "compra" -> "compras", "conta" -> "contas", etc.
    aliases = {
        "compra": "compras",
        "assinatura": "assinaturas",
        "conta": "contas",
        "imposto": "impostos",
        "outro_gasto": "outros_gastos",
        "outros_gasto": "outros_gastos",
        # Sinonimos comuns (para caber nas categorias permitidas).
        "gasolina": "transporte",
        "combustivel": "transporte",
        "posto": "transporte",
        "uber": "transporte",
        "99": "transporte",
        "taxi": "transporte",
        "pedagio": "transporte",
        "ifood": "alimentacao",
        "i_food": "alimentacao",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized


def sanitize_category_candidate(raw_value: str) -> str:
    value = raw_value.strip()
    if "'" in value:
        parts = [part for part in value.split("'") if part.strip()]
        if parts:
            value = parts[-1].strip()
    if '"' in value:
        parts = [part for part in value.split('"') if part.strip()]
        if parts:
            value = parts[-1].strip()
    return normalize_category(value)


def validate_category(kind: str, category: str) -> str:
    normalized = sanitize_category_candidate(category)
    allowed = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
    if normalized not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Categoria invalida para {kind}: '{category}'. "
                f"Categorias permitidas: {', '.join(sorted(allowed))}"
            ),
        )
    return normalized


def add_transaction(kind: str, data: FinanceCreate) -> dict:
    logger.info("Criando transacao", extra={"kind": kind, "phone": normalize_phone(data.phone)})
    occ_date = normalize_date(data.occurred_on)
    category = validate_category(kind, data.category)
    user_phone = normalize_phone(data.phone)
    ensure_user(user_phone)
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO transactions (kind, amount, category, description, occurred_on, created_at, user_phone)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, kind, amount, category, description, occurred_on, user_phone
            """,
            [
                kind,
                float(data.amount),
                category,
                data.description.strip(),
                occ_date,
                datetime.utcnow().isoformat(timespec="seconds"),
                user_phone,
            ],
        ).fetchone()
        if not row:
            logger.error("Falha ao inserir transacao.")
            raise HTTPException(status_code=500, detail="Falha ao criar transacao.")
        created = serialize_transaction(row)
        logger.info("Transacao criada", extra={"id": created["id"], "kind": created["kind"]})
        return created


def remove_transaction(transaction_id: int, phone: str | None = None) -> bool:
    user_phone = normalize_phone(phone) if phone else None
    logger.info(
        "Removendo transacao",
        extra={"transaction_id": transaction_id, "phone": user_phone or "all"},
    )
    with get_conn() as conn:
        if user_phone:
            cursor = conn.execute(
                "DELETE FROM transactions WHERE id = %s AND user_phone = %s",
                [transaction_id, user_phone],
            )
        else:
            cursor = conn.execute(
                "DELETE FROM transactions WHERE id = %s",
                [transaction_id],
            )
        return cursor.rowcount > 0


def get_period_bounds(period: str, ref_date: date) -> tuple[date, date]:
    if period == "day":
        return ref_date, ref_date
    if period == "week":
        start = ref_date - timedelta(days=ref_date.weekday())
        end = start + timedelta(days=6)
        return start, end
    if period == "month":
        start = ref_date.replace(day=1)
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1, day=1)
        else:
            next_month = start.replace(month=start.month + 1, day=1)
        end = next_month - timedelta(days=1)
        return start, end
    raise HTTPException(status_code=400, detail="Periodo invalido. Use day, week ou month.")


def resolve_report_kind(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_category(value)
    if normalized in {"expense", "expenses", "gasto", "gastos", "despesa", "despesas"}:
        return "expense"
    if normalized in {"income", "incomes", "receita", "receitas", "ganho", "ganhos"}:
        return "income"
    if normalized in {"all", "todos", "todas"}:
        return None
    raise HTTPException(
        status_code=400,
        detail="Filtro de tipo invalido. Use income, expense ou all.",
    )


def infer_report_kind_from_text(user_text: str) -> str | None:
    normalized = normalize_category(user_text)
    if any(token in normalized for token in ["gasto", "gastos", "despesa", "despesas"]):
        return "expense"
    if any(token in normalized for token in ["receita", "receitas", "ganho", "ganhos"]):
        return "income"
    return None


def build_report(
    period: str,
    reference_date: str | None,
    kind: str | None = None,
    phone: str | None = None,
) -> dict:
    logger.info(
        "Gerando relatorio",
        extra={"period": period, "kind": kind or "all", "phone": normalize_phone(phone) if phone else "all"},
    )
    ref = date.today() if not reference_date else normalize_date(reference_date)
    if isinstance(ref, str):
        ref = datetime.strptime(ref, "%Y-%m-%d").date()
    start, end = get_period_bounds(period, ref)

    where_kind = ""
    where_phone = ""
    params: list[str]
    if kind:
        where_kind = " AND kind = %s"
        params = [start.isoformat(), end.isoformat(), kind]
    else:
        params = [start.isoformat(), end.isoformat()]
    if phone:
        where_phone = " AND user_phone = %s"
        params.append(normalize_phone(phone))

    with get_conn() as conn:
        query = f"""
            SELECT id, kind, amount, category, description, occurred_on, user_phone
            FROM transactions
            WHERE occurred_on >= %s AND occurred_on <= %s
            {where_kind}
            {where_phone}
            ORDER BY occurred_on DESC, id DESC
            """
        rows = conn.execute(
            query,
            params,
        ).fetchall()

    items = [serialize_transaction(row) for row in rows]
    total_income = sum(item["amount"] for item in items if item["kind"] == "income")
    total_expense = sum(item["amount"] for item in items if item["kind"] == "expense")
    balance = total_income - total_expense

    return {
        "period": period,
        "kind_filter": kind or "all",
        "phone_filter": normalize_phone(phone) if phone else "all",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "total_income": round(total_income, 2),
        "total_expense": round(total_expense, 2),
        "balance": round(balance, 2),
        "transactions": items,
    }


def serialize_transaction(row: dict) -> dict:
    occurred_on = row["occurred_on"]
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "amount": float(row["amount"]),
        "category": row["category"],
        "description": row["description"] or "",
        "occurred_on": occurred_on.isoformat() if hasattr(occurred_on, "isoformat") else str(occurred_on),
        "phone": row.get("user_phone"),
    }


def run_transcription(audio_bytes: bytes, suffix: str) -> str:
    temp_path = None
    try:
        with NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(audio_bytes)
            temp_path = temp_file.name
        result = get_whisper_model().transcribe(temp_path, language="pt")
        return result.get("text", "").strip()
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


def run_ollama(messages: list[dict], model_name: str, json_mode: bool = False) -> str:
    started_at = time.perf_counter()
    logger.info(
        "Chamando Ollama",
        extra={
            "model": model_name,
            "stream_mode": OLLAMA_DEBUG_STREAM,
            "json_mode": json_mode,
        },
    )
    if DEBUG_PAYLOADS or OLLAMA_DEBUG_STREAM:
        # Evita logar prompt enorme; mostramos só preview.
        system_preview = ""
        user_preview = ""
        for m in messages:
            if m.get("role") == "system":
                system_preview = (m.get("content") or "")[:300]
            if m.get("role") == "user":
                user_preview = (m.get("content") or "")[:300]
        logger.info(
            "Ollama payload (preview)",
            extra={
                "model": model_name,
                "messages_roles": [m.get("role") for m in messages],
                "system_preview": system_preview,
                "user_preview": user_preview,
                "options": {
                    "temperature": OLLAMA_TEMPERATURE,
                    "num_predict": OLLAMA_NUM_PREDICT,
                    "num_ctx": OLLAMA_NUM_CTX,
                    "num_thread": OLLAMA_NUM_THREAD,
                },
            },
        )
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": OLLAMA_DEBUG_STREAM,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": OLLAMA_TEMPERATURE,
            "num_predict": OLLAMA_NUM_PREDICT,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_thread": OLLAMA_NUM_THREAD,
        },
    }
    if json_mode:
        payload["format"] = "json"
    req = request.Request(
        url=f"{OLLAMA_BASE_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            if OLLAMA_DEBUG_STREAM:
                chunks: list[str] = []
                chunk_count = 0
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    part = json.loads(line)
                    piece = (part.get("message", {}) or {}).get("content", "")
                    if piece:
                        chunks.append(piece)
                        chunk_count += 1
                        if chunk_count % 10 == 0:
                            logger.info(
                                "Ollama stream parcial",
                                extra={
                                    "chunks": chunk_count,
                                    "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                                },
                            )
                    if part.get("done"):
                        logger.info(
                            "Ollama stream finalizado",
                            extra={
                                "model": model_name,
                                "chunks": chunk_count,
                                "total_duration_ns": part.get("total_duration"),
                                "eval_duration_ns": part.get("eval_duration"),
                                "prompt_eval_count": part.get("prompt_eval_count"),
                                "eval_count": part.get("eval_count"),
                                "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                            },
                        )
                        if (DEBUG_PAYLOADS or OLLAMA_DEBUG_STREAM) and chunks:
                            logger.info(
                                "Ollama resposta (preview)",
                                extra={
                                    "response_preview": "".join(chunks)[:300],
                                    "response_len": len("".join(chunks)),
                                },
                            )
                        break
                return "".join(chunks).strip()

            body = json.loads(response.read().decode("utf-8"))
            logger.info(
                "Ollama resposta recebida",
                extra={
                    "model": model_name,
                    "total_duration_ns": body.get("total_duration"),
                    "eval_duration_ns": body.get("eval_duration"),
                    "prompt_eval_count": body.get("prompt_eval_count"),
                    "eval_count": body.get("eval_count"),
                    "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
                },
            )
            content = (body.get("message", {}) or {}).get("content", "").strip()
            if (DEBUG_PAYLOADS or OLLAMA_DEBUG_STREAM):
                logger.info(
                    "Ollama resposta (preview)",
                    extra={
                        "response_preview": content[:300],
                        "response_len": len(content),
                    },
                )
            return content
    except TimeoutError as exc:
        logger.exception("Timeout ao chamar Ollama")
        raise HTTPException(
            status_code=504,
            detail=(
                f"Ollama demorou mais que {OLLAMA_TIMEOUT_SECONDS}s. "
                "Aumente OLLAMA_TIMEOUT_SECONDS no .env, use modelo menor, ou reduza OLLAMA_NUM_PREDICT/OLLAMA_NUM_CTX."
            ),
        ) from exc
    except socket.timeout as exc:
        logger.exception("Timeout (socket) ao chamar Ollama")
        raise HTTPException(
            status_code=504,
            detail=(
                f"Ollama demorou mais que {OLLAMA_TIMEOUT_SECONDS}s. "
                "Aumente OLLAMA_TIMEOUT_SECONDS no .env, use modelo menor, ou reduza OLLAMA_NUM_PREDICT/OLLAMA_NUM_CTX."
            ),
        ) from exc
    except error.URLError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Nao foi possivel conectar no Ollama. "
                f"Verifique OLLAMA_BASE_URL ({OLLAMA_BASE_URL}), `ollama serve` e `ollama pull` do modelo."
            ),
        ) from exc


def parse_agent_plan(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json\n", "", 1).strip()
    try:
        parsed = json.loads(text)
        return coerce_agent_plan(parsed)
    except json.JSONDecodeError as exc:
        # Fallback para modelos pequenos que respondem com aspas simples
        # ou com texto extra junto do objeto.
        repaired = try_parse_lenient_json(text)
        if repaired is not None:
            return coerce_agent_plan(repaired)
        raise HTTPException(
            status_code=500,
            detail=f"Resposta do agente nao veio em JSON valido: {raw}",
        ) from exc


def try_parse_lenient_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = text[start : end + 1]
    try:
        value = ast.literal_eval(snippet)
        return value if isinstance(value, dict) else None
    except (ValueError, SyntaxError):
        return None


def coerce_agent_plan(plan: dict) -> dict:
    if not isinstance(plan, dict):
        return {
            "action": "clarify",
            "arguments": {},
            "message": "Nao entendi bem. Pode reformular em uma frase objetiva?",
            "requires_confirmation": False,
        }

    def get_nested(obj: dict, key: str):
        if not isinstance(obj, dict):
            return None
        return obj.get(key)

    def extract_amount(source: dict) -> float | None:
        if not isinstance(source, dict):
            return None
        candidates = [
            source.get("amount"),
            source.get("valor"),
            source.get("valor_compra"),
            source.get("valor_gasto"),
            source.get("valor_despesa"),
            source.get("valor_receita"),
            source.get("valor_income"),
        ]
        for v in candidates:
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    def extract_category(source: dict) -> str | None:
        if not isinstance(source, dict):
            return None
        candidates = [
            source.get("category"),
            source.get("categoria"),
            source.get("tipo"),
            source.get("categoria_compra"),
            source.get("categoria_gasto"),
            source.get("categoria_despesa"),
            source.get("tipo_gasto"),
            source.get("categoria_receita"),
            source.get("tipo_receita"),
        ]
        for v in candidates:
            if v is None:
                continue
            s = str(v).strip()
            if s:
                return s
        return None

    # Mapeia respostas comuns de modelos menores para o schema esperado.
    if "action" not in plan:
        amount = extract_amount(plan)
        inferred_action = "clarify"
        if amount is not None:
            inferred_action = "add_expense"
        if str(plan.get("tipo") or "").lower() in {"receita", "income"}:
            inferred_action = "add_income"

        plan = {
            "action": inferred_action,
            "arguments": {
                "amount": amount,
                "category": extract_category(plan),
                "description": plan.get("descricao") or plan.get("description"),
                "occurred_on": plan.get("data") or plan.get("occurred_on"),
                "transaction_id": plan.get("transaction_id"),
                "period": plan.get("period"),
                "report_kind": plan.get("report_kind"),
            },
            "message": plan.get("message") or "Acao processada.",
            "requires_confirmation": bool(plan.get("requires_confirmation", False)),
        }

    # Se a IA retornou action mas nao retornou arguments completos,
    # tenta reconstruir amount/category a partir de chaves alternativas.
    if "arguments" not in plan or not isinstance(plan.get("arguments"), dict):
        plan["arguments"] = {}
    args = plan["arguments"]

    # tenta extrair de "data"/"arguments" caso existam
    data_obj = get_nested(plan, "data") or {}
    if isinstance(data_obj, dict):
        # algumas respostas trazem os campos dentro de data
        if args.get("amount") is None:
            args["amount"] = extract_amount(data_obj) or extract_amount(plan)
        if not args.get("category"):
            args["category"] = extract_category(data_obj) or extract_category(plan)
    else:
        if args.get("amount") is None:
            args["amount"] = extract_amount(plan)
        if not args.get("category"):
            args["category"] = extract_category(plan)

    # também tenta dentro do próprio args se vier com chaves diferentes
    if args.get("amount") is None:
        args["amount"] = extract_amount(args)
    if not args.get("category"):
        args["category"] = extract_category(args)

    plan["arguments"] = args
    plan.setdefault("message", "Acao processada.")
    plan.setdefault("requires_confirmation", False)
    return plan


def build_agent_plan(user_text: str, model_name: str) -> dict:
    logger.info("Gerando plano do agente", extra={"model": model_name})
    system = """
Voce eh um roteador de acoes financeiras.
Responda SOMENTE JSON valido no formato:
{
  "action": "add_income|add_expense|remove_transaction|get_report|clarify",
  "arguments": {
    "amount": number|null,
    "category": string|null,
    "description": string|null,
    "occurred_on": "YYYY-MM-DD"|null,
    "transaction_id": integer|null,
    "period": "day|week|month"|null,
    "report_kind": "income|expense|all"|null
  },
  "message": "frase curta em portugues",
  "requires_confirmation": boolean
}
Use "clarify" quando faltar dado essencial.
Categorias permitidas para receita (add_income):
salario, freelance, investimentos, vendas, reembolso, bonus, outros_receitas
Categorias permitidas para despesa (add_expense):
alimentacao, moradia, transporte, saude, educacao, lazer, impostos, assinaturas, contas, compras, outros_gastos
Sempre retorne category em um desses valores exatos.
Nunca invente categoria, nunca responda texto livre em "category".
Se nao for possivel classificar com seguranca em uma categoria permitida, use action="clarify".
"""
    raw = run_ollama(
        messages=[
            {"role": "system", "content": system.strip()},
            {"role": "user", "content": user_text},
        ],
        model_name=model_name,
        json_mode=True,
    )
    return parse_agent_plan(raw)


def execute_agent_text(
    user_text: str, confirm: bool, model_name: str, phone: str | None = None
) -> dict:
    start = time.perf_counter()
    logger.info(
        "Executando agente",
        extra={
            "phone": normalize_phone(phone),
            "confirm": confirm,
            "text_preview": user_text[:120],
        },
    )
    plan = build_agent_plan(user_text, model_name)
    action = (plan.get("action") or "").strip()
    args = plan.get("arguments") or {}
    message = (plan.get("message") or "").strip() or "Acao processada."
    requires_confirmation = bool(plan.get("requires_confirmation"))

    if action == "clarify":
        logger.info("Agente pediu esclarecimento.")
        return {"ok": False, "action": action, "message": message, "needs_input": True}

    if action in {"add_income", "add_expense"}:
        amount = args.get("amount")
        category = args.get("category")
        if amount is None or not category:
            return {
                "ok": False,
                "action": "clarify",
                "message": "Faltou informar valor e categoria para lancar.",
                "needs_input": True,
            }
        payload = FinanceCreate(
            amount=float(amount),
            category=str(category),
            description=str(args.get("description") or ""),
            occurred_on=args.get("occurred_on"),
            phone=phone,
        )
        kind = "income" if action == "add_income" else "expense"
        cleaned_category = sanitize_category_candidate(payload.category)
        allowed = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
        if cleaned_category not in allowed:
            return {
                "ok": False,
                "action": "clarify",
                "message": (
                    "Categoria nao permitida. Escolha uma das categorias validas: "
                    + ", ".join(sorted(allowed))
                ),
                "needs_input": True,
                "allowed_categories": sorted(allowed),
            }
        payload.category = cleaned_category
        created = add_transaction(kind, payload)
        logger.info("Acao add executada", extra={"action": action, "elapsed_ms": int((time.perf_counter() - start) * 1000)})
        return {"ok": True, "action": action, "message": message, "data": created}

    if action == "remove_transaction":
        tx_id = args.get("transaction_id")
        if tx_id is None:
            return {
                "ok": False,
                "action": "clarify",
                "message": "Informe o ID da transacao que deseja remover.",
                "needs_input": True,
            }
        if requires_confirmation and not confirm:
            return {
                "ok": False,
                "action": action,
                "message": "Confirme a remocao enviando confirm=true.",
                "needs_confirmation": True,
                "arguments": {"transaction_id": tx_id},
            }
        deleted = remove_transaction(int(tx_id), phone=phone)
        if not deleted:
            logger.warning("Transacao nao encontrada para remocao", extra={"transaction_id": tx_id})
            return {
                "ok": False,
                "action": action,
                "message": f"Transacao {tx_id} nao encontrada.",
            }
        logger.info("Transacao removida", extra={"transaction_id": tx_id})
        return {
            "ok": True,
            "action": action,
            "message": message or f"Transacao {tx_id} removida.",
            "data": {"transaction_id": int(tx_id)},
        }

    if action == "get_report":
        period = str(args.get("period") or "month")
        report_kind = resolve_report_kind(args.get("report_kind"))
        if report_kind is None:
            report_kind = infer_report_kind_from_text(user_text)
        report = build_report(
            period=period,
            reference_date=args.get("occurred_on"),
            kind=report_kind,
            phone=phone,
        )
        logger.info("Relatorio gerado", extra={"elapsed_ms": int((time.perf_counter() - start) * 1000)})
        return {"ok": True, "action": action, "message": message, "data": report}

    return {
        "ok": False,
        "action": "clarify",
        "message": "Nao consegui identificar a acao. Pode reformular?",
        "needs_input": True,
    }


@app.on_event("startup")
def startup_event() -> None:
    logger.info(
        "API iniciada (Whisper carrega na primeira transcricao)",
        extra={
            "whisper_model": MODEL_NAME,
            "ollama_model": OLLAMA_MODEL,
            "ollama_base_url": OLLAMA_BASE_URL,
            "ollama_num_predict": OLLAMA_NUM_PREDICT,
            "ollama_num_ctx": OLLAMA_NUM_CTX,
            "ollama_num_thread": OLLAMA_NUM_THREAD,
            "ollama_debug_stream": OLLAMA_DEBUG_STREAM,
        },
    )
    init_db()
    logger.info("API pronta para receber requisicoes.")


@app.get("/")
def read_index() -> FileResponse:
    return FileResponse("static/index.html")


@app.post("/api/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)) -> dict:
    logger.info("Requisicao /api/transcribe recebida", extra={"audio_name": audio.filename})
    if not audio.filename:
        raise HTTPException(status_code=400, detail="Arquivo de audio sem nome.")
    extension = Path(audio.filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Formato nao suportado: " + ", ".join(sorted(ALLOWED_EXTENSIONS)),
        )
    content = await audio.read()
    text = run_transcription(content, extension)
    logger.info("Transcricao concluida", extra={"chars": len(text)})
    return {"text": text}


@app.post("/api/finance/income")
def create_income(payload: FinanceCreate) -> dict:
    logger.info("Endpoint /api/finance/income", extra={"phone": normalize_phone(payload.phone)})
    return {"ok": True, "data": add_transaction("income", payload)}


@app.post("/api/finance/expense")
def create_expense(payload: FinanceCreate) -> dict:
    logger.info("Endpoint /api/finance/expense", extra={"phone": normalize_phone(payload.phone)})
    return {"ok": True, "data": add_transaction("expense", payload)}


@app.delete("/api/finance/transaction/{transaction_id}")
def delete_transaction(transaction_id: int, phone: str | None = None) -> dict:
    logger.info("Endpoint delete transaction", extra={"transaction_id": transaction_id, "phone": normalize_phone(phone)})
    deleted = remove_transaction(transaction_id, phone=phone)
    if not deleted:
        raise HTTPException(status_code=404, detail="Transacao nao encontrada.")
    return {"ok": True, "transaction_id": transaction_id}


@app.get("/api/finance/report")
def finance_report(
    period: str = Query("month", pattern="^(day|week|month)$"),
    reference_date: str | None = None,
    kind: str = Query("all", pattern="^(all|income|expense)$"),
    phone: str | None = None,
) -> dict:
    logger.info(
        "Endpoint /api/finance/report",
        extra={"period": period, "kind": kind, "phone": normalize_phone(phone)},
    )
    kind_filter = None if kind == "all" else kind
    return {
        "ok": True,
        "data": build_report(
            period=period,
            reference_date=reference_date,
            kind=kind_filter,
            phone=phone,
        ),
    }


@app.get("/api/finance/categories")
def finance_categories() -> dict:
    return {
        "ok": True,
        "income_categories": sorted(INCOME_CATEGORIES),
        "expense_categories": sorted(EXPENSE_CATEGORIES),
    }


@app.post("/api/agent/execute")
def agent_execute(request_body: AgentExecuteRequest) -> dict:
    logger.info("Endpoint /api/agent/execute", extra={"phone": normalize_phone(request_body.phone)})
    if DEBUG_PAYLOADS:
        logger.info(
            "agent/execute request (preview)",
            extra={
                "text_preview": (request_body.text or "")[:300],
                "confirm": request_body.confirm,
                "model": request_body.model or OLLAMA_MODEL,
                "phone": normalize_phone(request_body.phone),
            },
        )
    return execute_agent_text(
        user_text=request_body.text.strip(),
        confirm=request_body.confirm,
        model_name=request_body.model or OLLAMA_MODEL,
        phone=request_body.phone,
    )


@app.post("/api/transcribe-and-agent")
async def transcribe_and_agent(
    audio: UploadFile = File(...),
    confirm: bool = Form(False),
    phone: str | None = Form(None),
) -> dict:
    logger.info(
        "Endpoint /api/transcribe-and-agent",
        extra={"audio_name": audio.filename, "phone": normalize_phone(phone)},
    )
    if not audio.filename:
        raise HTTPException(status_code=400, detail="Arquivo de audio sem nome.")
    extension = Path(audio.filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Formato nao suportado: " + ", ".join(sorted(ALLOWED_EXTENSIONS)),
        )
    content = await audio.read()
    text = run_transcription(content, extension)
    if DEBUG_PAYLOADS:
        logger.info(
            "transcribe-and-agent transcription (preview)",
            extra={"text_preview": (text or "")[:300], "confirm": confirm, "phone": normalize_phone(phone)},
        )
    agent_result = execute_agent_text(
        user_text=text,
        confirm=confirm,
        model_name=OLLAMA_MODEL,
        phone=phone,
    )
    if DEBUG_PAYLOADS:
        logger.info(
            "transcribe-and-agent agent_result (preview)",
            extra={
                "ok": agent_result.get("ok"),
                "action": agent_result.get("action"),
                "message_preview": (agent_result.get("message") or "")[:200],
            },
        )
    logger.info(
        "Fluxo transcribe-and-agent concluido",
        extra={"ok": agent_result.get("ok"), "action": agent_result.get("action")},
    )
    return {"transcription": text, "agent_result": agent_result}

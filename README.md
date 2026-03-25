# Finance Agent Local (Whisper + Ollama)

Projeto em Python com:
- transcricao local de audio para texto via Whisper
- agente local via Ollama para orquestrar acoes financeiras
- API (`FastAPI`) com endpoints financeiros em PostgreSQL
- front simples em HTML + JavaScript

## Requisitos

- Python 3.9+
- FFmpeg instalado no sistema (e no PATH)

## Instalacao

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Execucao (bot terminal)

```bash
python bot_whisper.py
```

No menu, voce pode:

1. Gravar audio no microfone e transcrever
2. Informar um arquivo de audio existente para transcrever

## Execucao (API + Front)

Instale dependencias:

```bash
python -m pip install -r requirements.txt
```

Suba o servidor:

```bash
python -m uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

Abra no navegador:

- http://localhost:8000

Endpoint de transcricao:

- `POST /api/transcribe`
- campo multipart: `audio`

Endpoint do agente orquestrador:

- `POST /api/agent/execute`
- body JSON:

```json
{
  "text": "adicionar despesa de 80 em mercado",
  "confirm": false,
  "model": "llama3.2:3b"
}
```

Fluxo completo (audio -> texto -> IA):

- `POST /api/transcribe-and-agent`
- campo multipart: `audio`

## Endpoints financeiros

- `POST /api/finance/income`
- `POST /api/finance/expense`
- `DELETE /api/finance/transaction/{transaction_id}`
- `GET /api/finance/report?period=day|week|month&kind=all|income|expense`
- `GET /api/finance/categories`

Exemplo `POST /api/finance/expense`:

```json
{
  "amount": 120.5,
  "category": "alimentacao",
  "description": "almoco",
  "occurred_on": "2026-03-25"
}
```

## Categorias fixas (obrigatorias)

Receitas (`income`):

- `salario`
- `freelance`
- `investimentos`
- `vendas`
- `reembolso`
- `bonus`
- `outros_receitas`

Despesas (`expense`):

- `alimentacao`
- `moradia`
- `transporte`
- `saude`
- `educacao`
- `lazer`
- `impostos`
- `assinaturas`
- `contas`
- `compras`
- `outros_gastos`

Se a categoria enviada nao estiver na lista, a API retorna erro `400`.

## IA local gratuita (Ollama)

Instale o Ollama e rode localmente:

- [https://ollama.com/download](https://ollama.com/download)

No terminal:

```bash
ollama serve
ollama pull llama3.2:3b
```

Observacao:

- O endpoint da IA chama `http://127.0.0.1:11434` (servidor local do Ollama).
- Nao precisa de chave de API paga.

## Prisma + PostgreSQL

Prisma foi configurado com datasource PostgreSQL em `prisma/schema.prisma`.

Modelos criados:

- `Transaction` (tabela `transactions`)
- `TransactionKind` (`income`, `expense`)
- `TransactionCategory` (categorias fixas de receita e despesa)

Configure sua conexao no arquivo `.env`:

```bash
DATABASE_URL="postgresql://usuario:senha@localhost:5432/finance_ia?schema=public"
```

Comandos:

```bash
npm run prisma:migrate -- --name init
npm run prisma:generate
npm run prisma:studio
```

## Bot WhatsApp (whatsapp-web.js)

Arquivo: `whatsapp-bot.js`

Fluxo:

- recebe mensagens no WhatsApp
- processa somente audio (`ptt`/`audio`)
- ignora texto para evitar loop
- responde somente para grupos permitidos (`ALLOWED_GROUP_IDS`)
- identifica usuario pelo telefone (`message.from`)
- envia para a API:
  - audio -> `POST /api/transcribe-and-agent`
- responde no WhatsApp com o resultado

Variaveis opcionais:

```bash
API_BASE_URL=http://127.0.0.1:8000
OLLAMA_MODEL=llama3.2:3b
ALLOWED_GROUP_IDS=120363407622971994@g.us
```

Rodar bot:

```bash
npm run bot:whatsapp
```

Na primeira execucao, escaneie o QR code no terminal.
As sessoes ficam salvas localmente (LocalAuth), entao nao precisa escanear sempre.

## Deploy EC2 com 1 comando

No EC2 (Ubuntu), com projeto ja copiado em `/home/ubuntu/finance-ia` e `.env` preenchido:

```bash
cd /home/ubuntu/finance-ia
sudo bash deploy/ec2-one-command.sh
```

Para habilitar HTTPS automatico (Certbot):

```bash
cd /home/ubuntu/finance-ia
sudo DOMAIN=api.seudominio.com CERTBOT_EMAIL=voce@seudominio.com ENABLE_HTTPS=true bash deploy/ec2-one-command.sh
```

O script faz automaticamente:

- instala Python/Node/Nginx/FFmpeg
- instala dependencias Python e Node
- aplica `prisma migrate deploy`
- cria services `systemd`:
  - `finance-api`
  - `finance-whatsapp`
- configura Nginx como proxy para `127.0.0.1:8000`
- sobe tudo

Comandos uteis no servidor:

```bash
journalctl -u finance-api -f
journalctl -u finance-whatsapp -f
systemctl status finance-api
systemctl status finance-whatsapp
```

Na primeira execucao do bot, o QR fica em:

```bash
/home/ubuntu/finance-ia/whatsapp-qr.png
```

## O que voce precisa preencher (chaves/segredos)

No arquivo `.env` do servidor:

- `DATABASE_URL`: string de conexao do PostgreSQL
- `ALLOWED_GROUP_IDS`: ID do grupo permitido no WhatsApp
- `API_BASE_URL`: normalmente `http://127.0.0.1:8000`
- `OLLAMA_MODEL`: ex. `llama3.2:3b`
- `DEBUG_LOGS`: `true` ou `false`

Exemplo:

```bash
DATABASE_URL="postgresql://usuario:senha@host:5432/db?sslmode=require"
ALLOWED_GROUP_IDS="120363407622971994@g.us"
API_BASE_URL="http://127.0.0.1:8000"
OLLAMA_MODEL="llama3.2:3b"
DEBUG_LOGS="true"
```

Para HTTPS no deploy (na linha de comando, nao no .env):

- `DOMAIN`: dominio apontando para o IP do EC2
- `CERTBOT_EMAIL`: email para certificados
- `ENABLE_HTTPS=true`

## Observacoes

- Na primeira execucao, o modelo Whisper sera baixado automaticamente.
- O idioma da transcricao esta configurado como portugues (`language="pt"`).

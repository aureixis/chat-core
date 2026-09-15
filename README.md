# Lamya API

FastAPI backend for translated messaging. PostgreSQL stores users, approved connections, conversations, messages, and admin translation settings.

## Run locally

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
docker compose up -d postgres
uvicorn app.main:app --reload
```

The API is available at `http://localhost:8000`; interactive documentation is at `/docs`.

The first startup creates the administrator from `ADMIN_EMAIL` and `ADMIN_PASSWORD`. Set `TRANSLATION_PROVIDER=local` for development, or use `openai_compatible` with an OpenAI-compatible `/chat/completions` endpoint. The local provider prefixes translated output with the target language so the full workflow can be tested without an LLM key.

Administrators configure the provider through `PUT /api/admin/translation` with a bearer token:

```json
{
	"provider": "openai_compatible",
	"api_url": "https://api.openai.com/v1",
	"api_key": "server-side-secret",
	"model": "gpt-4o-mini",
	"system_prompt": "Translate naturally and preserve the speaker's tone. Return only the translated text."
}
```

The configured prompt is sent as the LLM system message. Source and target languages are supplied separately for each message, so administrators can change translation style without changing application code.

## Deploy to Railway

Create a Railway project with these services:

1. Add a PostgreSQL service.
2. Add the `chat-core` directory as the backend service root.
3. Add the `chat-web-client` directory as the frontend service root.

Backend variables:

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
JWT_SECRET=<long-random-secret>
CORS_ORIGINS=https://lamya.aureixis.com
ADMIN_EMAIL=<admin-email>
ADMIN_PASSWORD=<strong-admin-password>
TRANSLATION_PROVIDER=openai_compatible
TRANSLATION_API_URL=https://api.openai.com/v1
TRANSLATION_API_KEY=<server-side-api-key>
TRANSLATION_MODEL=gpt-4o-mini
```

After the backend is deployed, copy its public HTTPS domain into the frontend variable:

```text
VITE_API_URL=https://<your-backend-domain>
```

Railway rebuilds the frontend when `VITE_API_URL` changes. The frontend automatically converts the HTTPS API URL to `wss://` for chat WebSockets. Generate public domains for both app services, then set `CORS_ORIGINS` to the exact frontend origin without a trailing slash.

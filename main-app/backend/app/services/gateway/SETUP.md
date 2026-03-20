# Gateway Setup Guide

## Zuplo Configuration

### Routes to create in Zuplo dashboard:

1. **POST /v1/chat/completions**
   - Handler: URL Rewrite → `https://api.openai.com/v1/chat/completions`
   - Policies:
     - API Key Authentication (inbound)
     - Rate Limiting: 60 req/min per consumer (inbound)
     - Add Upstream Auth: inject `Authorization: Bearer sk-YOUR-OPENAI-KEY` (inbound)
     - Request/Response Logging (inbound + outbound)
     - Request body validation: reject if `max_tokens > 4096` (inbound)

2. **POST /v1/embeddings**
   - Handler: URL Rewrite → `https://api.openai.com/v1/embeddings`
   - Same policies as above

### Environment Variables (Retrace .env):
```
GATEWAY_PROVIDER=zuplo
GATEWAY_BASE_URL=https://llm.lumenatech.ai
GATEWAY_ADMIN_KEY=your-zuplo-admin-api-key
```

---

## Cloudflare Configuration

### DNS Setup:
1. Add `lumenatech.ai` to Cloudflare (change nameservers at Infomaniak)
2. Add CNAME record:
   - Name: `llm`
   - Target: (Zuplo custom domain target from Zuplo dashboard)
   - Proxy: ON (orange cloud)

### Security Rules:
1. WAF → Custom Rules:
   - Block requests to `llm.lumenatech.ai` without `Authorization` header
2. Security → DDoS:
   - Enabled (default)
3. Security → Bots:
   - Enable Bot Fight Mode
4. Rate Limiting:
   - Global cap: 1000 req/min to `llm.lumenatech.ai` (DDoS backstop)

---

## Vendor Migration Checklist

To switch from Zuplo to another provider (e.g. Kong):

1. Implement the new provider class in `app/services/gateway/`
2. Set up equivalent routes and policies in the new vendor
3. Run migration script to create consumers in the new vendor
4. Update DNS: change `llm.lumenatech.ai` CNAME to new vendor target
5. Change `.env`: `GATEWAY_PROVIDER=kong`
6. Restart backend

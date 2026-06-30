# Workspace Agent MCP Callback

The Workspace Agents trigger API accepts a run and can return a `conversation_url`, but it does not expose the final agent answer by API. Because of that, the reliable pattern is:

1. The backend triggers the Workspace Agent and stores `ai_validation.status = "queued"`.
2. The Workspace Agent uses a custom MCP tool to send its final verdict back to the backend.
3. The frontend polls the saved status and renders `approved` or `rejected`.

## Internal callback endpoint

`POST /api/internal/ai-validation`

Headers:

- `Authorization: Bearer <AI_VALIDATION_CALLBACK_TOKEN>`

or

- `X-AI-Validation-Token: <AI_VALIDATION_CALLBACK_TOKEN>`

Body example:

```json
{
  "systematic_rfq_id": "26ABC-BRU-00",
  "status": "approved",
  "approved": true,
  "message": "RFQ approved.",
  "discussion": "All required business fields are present.",
  "fields_to_correct": [],
  "conversation_url": "https://chatgpt.com/c/123",
  "source": "workspace_agent_mcp"
}
```

`status` can be sent as `approved`, `rejected`, `completed`, `queued`, or `processing`.

## Frontend status endpoint

`GET /api/rfq/{rfq_id}/ai-validation-status`

This returns the normalized status saved in `rfq.rfq_data.ai_validation`.

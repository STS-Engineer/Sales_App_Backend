# Workspace Agent MCP Callback

The Workspace Agents trigger API accepts a run and can return a `conversation_url`, but it does not expose the final agent answer by API. It also accepts text input only, not a direct PDF attachment in the trigger body. Because of that, the reliable pattern is:

1. The backend triggers the Workspace Agent and stores `ai_validation.status = "queued"`.
2. When an RFQ attachment must be read, the Workspace Agent calls `analyze_rfq_blob_attachment_with_openai` so the original Azure Blob file is forwarded to OpenAI as a true `input_file`.
3. The Workspace Agent uses `save_ai_validation_result` to send its final verdict back to the backend.
4. The frontend polls the saved status and renders `approved` or `rejected`.

## Agent file-reading rule

For API-triggered RFQs, the agent should not treat raw blob URLs or backend-extracted text as the primary source of truth for the drawing.

Preferred flow:

1. Identify the relevant `rfq_files[]` entry.
2. Call `analyze_rfq_blob_attachment_with_openai` with `systematic_rfq_id` and `file_id`.
3. Use that tool result as the primary source for the plan / drawing review.
4. Only report a file as unreadable when the MCP tool itself returns an error.

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

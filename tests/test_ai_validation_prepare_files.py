import pytest

from app.services.ai_validation import prepare_rfq_files_for_agent


@pytest.mark.asyncio
async def test_prepare_rfq_files_for_agent_prefers_file_id_tool_args():
    files = [
        {
            "id": "file-123",
            "filename": "drawing.pdf",
            "blob_name": "rfq-files/drawing.pdf",
        }
    ]

    prepared = await prepare_rfq_files_for_agent(
        files,
        backend_base_url="https://backend.example.com/",
    )

    assert prepared[0]["proxy_url"] == "https://backend.example.com/api/rfq/files/file-123/proxy"
    assert prepared[0]["agent_file_url"] == prepared[0]["proxy_url"]
    assert prepared[0]["agent_file_tool"]["name"] == "analyze_rfq_blob_attachment_with_openai"
    assert prepared[0]["agent_file_tool"]["arguments"] == {"file_id": "file-123"}
    assert prepared[0]["agent_file_text_status"] == "mcp_openai_input_file"
    assert prepared[0]["agent_file_text"] == ""


@pytest.mark.asyncio
async def test_prepare_rfq_files_for_agent_falls_back_to_filename_contains():
    files = [
        {
            "filename": "assy-plan-v2.pdf",
            "blob_name": "rfq-files/assy-plan-v2.pdf",
            "url": "https://blob.example.com/assy-plan-v2.pdf",
        }
    ]

    prepared = await prepare_rfq_files_for_agent(
        files,
        backend_base_url="https://backend.example.com",
    )

    assert "proxy_url" not in prepared[0]
    assert prepared[0]["agent_file_url"] == "https://blob.example.com/assy-plan-v2.pdf"
    assert prepared[0]["agent_file_tool"]["arguments"] == {
        "filename_contains": "assy-plan-v2.pdf"
    }

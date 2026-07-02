import pytest

from app.services.ai_validation import (
    build_workspace_agent_input,
    prepare_rfq_files_for_agent,
    prepare_rfq_payload_for_agent,
)


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


def test_prepare_rfq_payload_for_agent_marks_matching_multi_year_quantity_as_cumulative():
    payload = prepare_rfq_payload_for_agent(
        {
            "annual_volume": 3000000,
            "products": [
                {
                    "part_number": "1004321000",
                    "quantity": 3000000,
                }
            ],
            "volumes": [
                {
                    "volumes": {
                        "2026": 480000,
                        "2027": 480000,
                        "2028": 600000,
                        "2029": 720000,
                        "2030": 720000,
                    }
                }
            ],
        }
    )

    assert "annual_volume" not in payload
    assert payload["agent_legacy_quantity_mirrors"] == {"annual_volume": 3000000}
    assert payload["agent_volume_rows"][0]["quantity_basis"] == (
        "cumulative_program_total_matching_yearly_profile"
    )
    assert payload["agent_volume_rows"][0]["annual_volume_confirmation_required"] is False
    assert payload["products"][0]["agent_quantity_basis"] == (
        "cumulative_program_total_matching_yearly_profile"
    )
    assert payload["products"][0]["agent_yearly_total_quantity"] == 3000000


def test_build_workspace_agent_input_instructs_agent_not_to_ask_for_annual_vs_cumulative_confirmation():
    message = build_workspace_agent_input(
        {
            "products": [{"part_number": "PN-1", "quantity": 3000000}],
            "volumes": [
                {
                    "volumes": {
                        "2026": 480000,
                        "2027": 480000,
                        "2028": 600000,
                        "2029": 720000,
                        "2030": 720000,
                    }
                }
            ],
        }
    )

    assert "do not ask the KAM to confirm whether the matching sum is annual or cumulative" in message
    assert "cumulative_program_total_matching_yearly_profile" in message

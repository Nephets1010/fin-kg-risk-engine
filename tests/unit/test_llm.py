"""Unit tests for the optional LLM client.

The LLM backend is an accelerator, never a dependency. These tests pin the
two properties that matter: it never runs without an explicit key, and a
failure degrades instead of propagating.
"""

from __future__ import annotations

import pytest

from fin_kg.config.settings import LLMSettings
from fin_kg.core.errors import LLMError
from fin_kg.research.llm import LLMClient, build_client


class TestBuildClient:
    def test_a_disabled_backend_yields_no_client(self) -> None:
        assert build_client(LLMSettings(enabled=False, api_key="")) is None

    def test_an_enabled_backend_without_a_key_yields_no_client(self) -> None:
        assert build_client(LLMSettings(enabled=True, api_key="")) is None

    def test_an_enabled_backend_with_a_key_yields_a_client(self) -> None:
        client = build_client(LLMSettings(enabled=True, api_key="sk-test-value"))
        assert client is not None
        assert client.enabled


class TestLLMClient:
    def test_a_disabled_client_refuses_to_run(self) -> None:
        client = LLMClient(LLMSettings(enabled=False, api_key=""))
        with pytest.raises(LLMError):
            client.complete("hello")

    def test_the_key_is_registered_for_masking(self) -> None:
        from fin_kg.core.logging import mask_secrets

        LLMClient(LLMSettings(enabled=True, api_key="sk-super-secret-key"))
        assert "sk-super-secret-key" not in mask_secrets("token=sk-super-secret-key")

    def test_a_transport_failure_is_reported_as_an_llm_error(self, monkeypatch) -> None:
        import httpx

        def boom(*_args, **_kwargs):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(httpx, "post", boom)
        client = LLMClient(LLMSettings(enabled=True, api_key="sk-test-value"))
        with pytest.raises(LLMError):
            client.complete("hello")

    def test_a_malformed_body_is_reported_as_an_llm_error(self, monkeypatch) -> None:
        import httpx

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"unexpected": "shape"}

        monkeypatch.setattr(httpx, "post", lambda *_a, **_k: FakeResponse())
        client = LLMClient(LLMSettings(enabled=True, api_key="sk-test-value"))
        with pytest.raises(LLMError):
            client.complete("hello")

    def test_a_successful_call_returns_the_text(self, monkeypatch) -> None:
        import httpx

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"choices": [{"message": {"content": "  答案  "}}]}

        captured: dict[str, object] = {}

        def fake_post(url, **kwargs):  # noqa: ARG001
            captured.update(kwargs)
            return FakeResponse()

        monkeypatch.setattr(httpx, "post", fake_post)
        client = LLMClient(LLMSettings(enabled=True, api_key="sk-test-value"))
        assert client.complete("hello") == "答案"

    def test_the_request_carries_the_authorization_header(self, monkeypatch) -> None:
        import httpx

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"choices": [{"message": {"content": "ok"}}]}

        captured: dict[str, object] = {}

        def fake_post(_url, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

        monkeypatch.setattr(httpx, "post", fake_post)
        client = LLMClient(LLMSettings(enabled=True, api_key="sk-test-value"))
        client.complete("hello")
        headers = captured["headers"]
        assert headers["Authorization"] == "Bearer sk-test-value"

    def test_complete_json_parses_the_answer(self, monkeypatch) -> None:
        import httpx

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"choices": [{"message": {"content": '{"answer": 42}'}}]}

        monkeypatch.setattr(httpx, "post", lambda *_a, **_k: FakeResponse())
        client = LLMClient(LLMSettings(enabled=True, api_key="sk-test-value"))
        assert client.complete_json("hello") == {"answer": 42}

    def test_complete_json_rejects_a_non_json_answer(self, monkeypatch) -> None:
        import httpx

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"choices": [{"message": {"content": "not json"}}]}

        monkeypatch.setattr(httpx, "post", lambda *_a, **_k: FakeResponse())
        client = LLMClient(LLMSettings(enabled=True, api_key="sk-test-value"))
        with pytest.raises(LLMError):
            client.complete_json("hello")


class TestKGQAFallback:
    def test_the_template_answer_is_used_when_the_llm_fails(self, seeded_repository) -> None:
        """A broken LLM must not break the answer.

        The KGQA renderer catches the LLM failure and falls back to the
        deterministic template, so the answer and its evidence survive.
        """
        from fin_kg.research.kgqa import KGQA

        class BrokenLLM:
            def complete(self, *_args, **_kwargs) -> str:
                raise LLMError("the model is down")

        qa = KGQA(seeded_repository, llm=BrokenLLM())
        answer = qa.ask("风险最高的前 3 家公司")
        assert answer.answer
        assert answer.evidence

    def test_the_llm_phrase_is_used_when_it_works(self, seeded_repository) -> None:
        from fin_kg.research.kgqa import KGQA

        class WorkingLLM:
            def complete(self, prompt: str, **_kwargs) -> str:
                return "润色后的答案"

        qa = KGQA(seeded_repository, llm=WorkingLLM())
        answer = qa.ask("风险最高的前 3 家公司")
        assert answer.answer == "润色后的答案"
        # The evidence still comes from the graph, never from the model.
        assert answer.evidence

    def test_the_llm_never_sees_a_cypher_statement(self, seeded_repository) -> None:
        """The model must not be able to influence the query.

        The prompt carries the facts and the question, never a Cypher
        statement, so a prompt injection cannot reach the graph.
        """
        from fin_kg.research.kgqa import KGQA

        captured: list[str] = []

        class CapturingLLM:
            def complete(self, prompt: str, **_kwargs) -> str:
                captured.append(prompt)
                return "ok"

        qa = KGQA(seeded_repository, llm=CapturingLLM())
        companies = seeded_repository.find_nodes("Company")
        assert companies
        qa.ask(f"{companies[0]['name']}的担保链")
        assert captured
        prompt = captured[0]
        assert "MATCH" not in prompt.upper()
        assert "RETURN" not in prompt.upper()

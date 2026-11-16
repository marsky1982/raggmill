"""连接器层：统一对外部服务的 HTTP 访问。

当前只有 RAGFlow 客户端（`ragas_adapter/ragflow.py`）使用；
LLM / Embedding 客户端**不并入**这里，见 `connectors/http.py` 的说明。
"""

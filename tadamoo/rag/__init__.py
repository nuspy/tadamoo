"""Knowledge archives: documents cut into passages, searched by words and, when an embedding model is installed, by meaning too."""
from tadamoo.rag.embed import Embedder, EmbedderProvider
from tadamoo.rag.errors import RagError
from tadamoo.rag.store import LIMITS, RagStore

__all__ = ["Embedder", "EmbedderProvider", "LIMITS", "RagError", "RagStore"]

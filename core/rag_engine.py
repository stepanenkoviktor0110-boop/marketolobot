# core/rag_engine.py
import hashlib
import re
import logging
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger("pmf-web")


class RAGEngine:
    def __init__(self, projects_root: Path):
        self.projects_root = projects_root
        self._embedder = None  # lazy-loaded on first use

        chroma_path = projects_root.parent / "chroma_db"
        chroma_path.mkdir(exist_ok=True)

        import chromadb
        self._chroma_path = str(chroma_path)
        self.client = chromadb.PersistentClient(path=self._chroma_path)

    @property
    def embedder(self):
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer('all-MiniLM-L6-v2')
        return self._embedder

    def _safe_name(self, project: str) -> str:
        safe = re.sub(r'[^a-zA-Z0-9_]', '_', project)
        return f"pmf_{safe}"

    def _get_collection(self, project: str):
        return self.client.get_or_create_collection(self._safe_name(project))

    def get_stats(self, project: str) -> Dict:
        try:
            col = self._get_collection(project)
            return {"project": project, "documents": col.count(), "status": "indexed"}
        except Exception:
            return {"project": project, "documents": 0, "status": "not_indexed"}

    def index_project(self, project: str) -> Dict:
        project_path = self.projects_root / project
        if not project_path.exists():
            raise ValueError(f"Project {project} not found")

        collection = self._get_collection(project)
        docs, metas, ids = [], [], []

        # 1. Context digest
        digest = project_path / "docs" / "context_digest.md"
        if digest.exists():
            content = digest.read_text(encoding="utf-8")
            for i, chunk in enumerate(self._chunk_text(content)):
                doc_id = f"digest_{i}_{hashlib.md5(chunk.encode()).hexdigest()[:6]}"
                docs.append(chunk)
                metas.append({"type": "context_digest", "project": project, "date": datetime.now().isoformat()})
                ids.append(doc_id)

        # 2. Stage outputs
        output_dir = project_path / "output"
        if output_dir.exists():
            for f in output_dir.glob("*_final.md"):
                content = f.read_text(encoding="utf-8")
                for i, chunk in enumerate(self._chunk_text(content, chunk_size=300, overlap=30)):
                    doc_id = f"stage_{f.stem}_{i}_{hashlib.md5(chunk.encode()).hexdigest()[:6]}"
                    docs.append(chunk)
                    metas.append({"type": f"stage_{f.stem}", "project": project, "file": f.name})
                    ids.append(doc_id)

        # 3. Inbox notes
        inbox_dir = project_path / "inbox"
        if inbox_dir.exists():
            for f in inbox_dir.glob("*.md"):
                content = f.read_text(encoding="utf-8")
                doc_id = f"inbox_{f.stem}_{hashlib.md5(content.encode()).hexdigest()[:6]}"
                docs.append(content[:1000])
                metas.append({"type": "inbox_note", "project": project, "file": f.name})
                ids.append(doc_id)

        if docs:
            embeddings = self.embedder.encode(docs, show_progress_bar=False, batch_size=32)
            # upsert handles re-indexing: existing IDs are replaced, new ones are added
            collection.upsert(
                embeddings=embeddings.tolist(),
                documents=docs,
                metadatas=metas,
                ids=ids
            )

        return {"indexed_docs": len(docs), "project": project}

    def search(self, project: str, query: str, top_k: int = 5, filter_type: Optional[str] = None) -> List[Dict]:
        collection = self._get_collection(project)
        if collection.count() == 0:
            return []

        query_embedding = self.embedder.encode([query]).tolist()[0]
        where_filter = {"project": project}
        if filter_type:
            where_filter["type"] = filter_type

        try:
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, collection.count()),
                where=where_filter,
                include=["documents", "metadatas", "distances"]
            )
        except Exception as e:
            # chromadb raises when where clause matches no documents
            logger.debug("RAG query returned no results (filter=%s): %s", filter_type, e)
            return []

        return [
            {
                "content": doc,
                "metadata": meta,
                "score": round(1 - dist, 3)
            }
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0]
            )
        ]

    def _chunk_text(self, text: str, chunk_size: int = 400, overlap: int = 50) -> List[str]:
        words = text.split()
        chunks = []
        step = max(1, chunk_size - overlap)
        for i in range(0, len(words), step):
            chunk = " ".join(words[i:i + chunk_size])
            if chunk.strip():
                chunks.append(chunk)
        return chunks

    def clear_project(self, project: str):
        try:
            self.client.delete_collection(self._safe_name(project))
        except Exception:
            pass

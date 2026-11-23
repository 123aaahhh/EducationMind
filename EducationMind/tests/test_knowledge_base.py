import sys
import types
import threading

try:
    import chromadb
except ModuleNotFoundError:
    sys.modules['chromadb'] = types.ModuleType('chromadb')

from tooling.knowledge_base import KnowledgeBase


class FakeCollection:
    def __init__(self):
        self.rows = {}
        self.calls = []

    def upsert(self, **kwargs):
        self.calls.append(kwargs)
        for i, doc, meta in zip(kwargs['ids'], kwargs['documents'], kwargs['metadatas']):
            self.rows[i] = (doc, meta)

    def get(self, where, include):
        rows = [(i, d, m) for i, (d, m) in self.rows.items() if all(m.get(k) == v for k, v in where.items())]
        return {'ids': [r[0] for r in rows], 'metadatas': [r[2] for r in rows]}

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)


def make_kb():
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._write_lock = threading.RLock()
    kb._collection = FakeCollection()
    return kb


def test_repeated_document_import_is_idempotent():
    kb = make_kb()
    doc = {'title': '退款政策', 'content': '7 天内可以申请退款。'}
    assert kb.add_documents([doc, doc]) == 1
    assert kb.add_documents([doc]) == 1
    assert len(kb._collection.rows) == 1


def test_shorter_document_replaces_chunks_and_legacy_data():
    kb = make_kb()
    kb._collection.rows['legacy'] = ('旧内容', {'title': '政策'})
    kb.add_documents([{'title': '政策', 'content': ('旧规则' * 100 + '。') * 5}])
    assert len(kb._collection.rows) > 1
    assert 'legacy' not in kb._collection.rows
    kb.add_documents([{'title': '政策', 'content': '新的政策'}])
    assert len(kb._collection.rows) == 1
    assert next(iter(kb._collection.rows.values()))[0] == '新的政策'


def test_explicit_ids_allow_documents_with_same_title():
    kb = make_kb()
    kb.add_documents([{'document_id': 'a', 'title': '政策', 'content': '甲'},
                      {'document_id': 'b', 'title': '政策', 'content': '乙'}])
    assert len(kb._collection.rows) == 2
    kb.add_documents([{'document_id': 'a', 'title': '改名', 'content': '新甲'}])
    assert len(kb._collection.rows) == 2
    assert {d for d, m in kb._collection.rows.values()} == {'新甲', '乙'}


def test_long_unpunctuated_text_respects_chunk_size():
    kb = make_kb()
    chunks = kb._chunk_text('长' * 1201, chunk_size=500)
    assert [len(c) for c in chunks] == [500, 500, 201]
    assert ''.join(chunks) == '长' * 1201


def test_structured_chunking_keeps_formula_table_and_code_separate():
    kb = make_kb()
    content = (
        '开场白。\n\n$$\nE = mc^2\n$$\n\n'
        '| 名称 | 值 |\n| --- | --- |\n| a | 1 |\n\n'
        '```python\ndef f():\n    return 1\n```\n\n结尾。'
    )
    chunks = [chunk.strip() for chunk in kb._chunk_document(content, chunk_size=30)]
    assert any(chunk.startswith('$$') for chunk in chunks)
    assert any(chunk.startswith('| 名称 |') for chunk in chunks)
    assert any('def f():' in chunk for chunk in chunks)


def test_chunks_record_chapter_path_and_full_metadata():
    kb = make_kb()
    kb.add_documents([{
        'title': '二次函数顶点公式',
        'content': '顶点坐标为 (-b/2a, (4ac-b²)/4a)。',
        'subject': '高中数学', 'grade': '高一', 'chapter': '函数',
        'knowledge_point': '二次函数', 'subsection': '顶点公式',
        'source_name': '人教版高中数学必修一', 'publisher': '人民教育出版社',
        'textbook_version': '人教版2019', 'region': '全国',
        'effective_at': '2025-09-01', 'expires_at': '2030-08-31',
    }])
    doc, meta = next(iter(kb._collection.rows.values()))
    path = '高中数学 / 高一 / 函数 / 二次函数 / 顶点公式'
    assert meta['chapter_path'] == path
    assert doc.startswith(f'{path}\n')
    assert meta['resource_type'] == 'textbook'
    assert meta['source_name'] == '人教版高中数学必修一'
    assert meta['textbook_version'] == '人教版2019'
    assert meta['expires_ts'] > 0 and meta['effective_ts'] > 0


class SearchCollection(FakeCollection):
    """支持 count/query 的假集合，用于验证检索过滤。"""

    def count(self):
        return len(self.rows)

    def query(self, query_texts, n_results):
        rows = list(self.rows.values())[:n_results]
        return {
            'documents': [[doc for doc, _ in rows]],
            'metadatas': [[meta for _, meta in rows]],
            'distances': [[0.1] * len(rows)],
        }


def test_expired_knowledge_is_excluded_from_search():
    kb = make_kb()
    kb._collection = SearchCollection()
    kb.add_documents([
        {'title': '过期报名时间', 'content': '旧报名时间。', 'resource_type': 'policy',
         'effective_at': '2019-01-01', 'expires_at': '2020-01-01'},
        {'title': '当前报名时间', 'content': '新报名时间。', 'resource_type': 'policy',
         'effective_at': '2025-01-01', 'expires_at': '2030-01-01'},
    ])
    results = kb.search('报名时间', top_k=5)
    assert [item['title'] for item in results] == ['当前报名时间']
    assert '_expires_ts' not in results[0]


class FakeClient:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, name, metadata=None):
        return self.collections.setdefault(name, SearchCollection())


def test_resource_types_use_separate_collections():
    kb = make_kb()
    kb._collection = SearchCollection()
    kb._client = FakeClient()
    kb._collections = {'textbook': kb._collection}
    kb.add_documents([
        {'title': '教材文档', 'content': '甲', 'resource_type': 'textbook'},
        {'title': '政策文档', 'content': '乙', 'resource_type': 'policy'},
    ])
    assert len(kb._collection.rows) == 1
    assert list(kb._collections['policy'].rows.values())[0][0] == '乙'
    assert kb.doc_count == 2


class HybridCollection(SearchCollection):
    """支持 get(include=...) 的假集合，用于验证 BM25 + 向量融合。"""

    def get(self, where=None, include=None):
        rows = [
            (row_id, doc, meta)
            for row_id, (doc, meta) in self.rows.items()
            if not where or all(meta.get(key) == value for key, value in where.items())
        ]
        return {
            'ids': [row[0] for row in rows],
            'documents': [row[1] for row in rows],
            'metadatas': [row[2] for row in rows],
        }


def test_vector_and_bm25_results_are_fused_by_rrf():
    kb = make_kb()
    kb._collection = HybridCollection()
    kb.add_documents([
        {'title': '函数定义', 'content': '使用 def 定义函数。'},
        {'title': 'Python 循环', 'content': 'for 循环遍历序列，while 循环依赖条件。'},
    ])

    results = kb.search('while', top_k=2)

    assert results[0]['title'] == 'Python 循环'
    assert results[0]['rrf_score'] > 0
    assert results[0]['bm25_score'] > 0


def test_new_collections_use_cosine_distance():
    assert KnowledgeBase._collection_metadata('测试')['hnsw:space'] == 'cosine'


def test_distance_to_score_handles_cosine_and_legacy_l2():
    kb = make_kb()
    cosine = types.SimpleNamespace(metadata={'hnsw:space': 'cosine'})
    legacy_l2 = types.SimpleNamespace(metadata={})

    assert abs(kb._distance_to_score(cosine, 0.2) - 0.8) < 1e-9
    assert abs(kb._distance_to_score(legacy_l2, 0.2) - 0.9) < 1e-9
    assert kb._distance_to_score(cosine, 2.0) == 0.0


def test_bge_pool_uses_cls_token_and_normalizes():
    import numpy as np
    from tooling.embeddings import OnnxEmbeddingFunction

    hidden = np.array(
        [
            [[3.0, 4.0], [100.0, 100.0]],
            [[0.0, 2.0], [1.0, 1.0]],
        ],
        dtype=np.float32,
    )
    pooled = OnnxEmbeddingFunction._pool(hidden)

    assert np.allclose(np.linalg.norm(pooled, axis=1), 1.0)
    assert np.allclose(pooled[0], [0.6, 0.8])

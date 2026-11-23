"""学习闭环存储：错题记录与知识点掌握度。

使用与对话记忆相同的 Redis 连接，按用户隔离：
  - learning:mistakes:{user}  最近错题（JSON 列表）
  - learning:mastery:{user}   知识点掌握度（Hash: 知识点 -> {correct,total}）
"""
import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional

MISTAKE_TTL = 90 * 86400
MASTERY_TTL = 90 * 86400
MAX_MISTAKES = 200


def _digest(user_id: str) -> str:
    return hashlib.sha256(str(user_id or "anonymous").encode()).hexdigest()


def _topic(value: Any) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:40] or "未分类"


class LearningStore:
    def __init__(self, redis_client):
        self._redis = redis_client

    @staticmethod
    def _mistakes_key(user_id: str) -> str:
        return f"learning:mistakes:{_digest(user_id)}"

    @staticmethod
    def _mastery_key(user_id: str) -> str:
        return f"learning:mastery:{_digest(user_id)}"

    async def add_mistake(
        self,
        user_id: str,
        question: str,
        answer: str = "",
        analysis: str = "",
        topic: str = "",
    ) -> Dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex,
            "question": str(question or "")[:2000],
            "answer": str(answer or "")[:4000],
            "analysis": str(analysis or "")[:2000],
            "topic": _topic(topic),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        key = self._mistakes_key(user_id)
        await self._redis.lpush(key, json.dumps(item, ensure_ascii=False))
        await self._redis.ltrim(key, 0, MAX_MISTAKES - 1)
        await self._redis.expire(key, MISTAKE_TTL)
        return item

    async def list_mistakes(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 50), MAX_MISTAKES))
        rows = await self._redis.lrange(self._mistakes_key(user_id), 0, limit - 1)
        items = []
        for row in rows:
            try:
                items.append(json.loads(row))
            except (TypeError, ValueError):
                continue
        return items

    async def delete_mistake(self, user_id: str, mistake_id: str) -> bool:
        key = self._mistakes_key(user_id)
        rows = await self._redis.lrange(key, 0, -1)
        removed = False
        for row in rows:
            try:
                item = json.loads(row)
            except (TypeError, ValueError):
                continue
            if item.get("id") == mistake_id:
                await self._redis.lrem(key, 1, row)
                removed = True
                break
        return removed

    async def record_attempt(self, user_id: str, topic: str, correct: bool) -> Dict[str, Any]:
        name = _topic(topic)
        key = self._mastery_key(user_id)
        raw = await self._redis.hget(key, name)
        try:
            state = json.loads(raw) if raw else {}
        except ValueError:
            state = {}
        total = int(state.get("total", 0)) + 1
        hits = int(state.get("correct", 0)) + (1 if correct else 0)
        state = {
            "correct": hits,
            "total": total,
            "mastery": round(hits / total, 3),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        await self._redis.hset(key, name, json.dumps(state, ensure_ascii=False))
        await self._redis.expire(key, MASTERY_TTL)
        return {"topic": name, **state}

    async def mastery(self, user_id: str) -> List[Dict[str, Any]]:
        raw = await self._redis.hgetall(self._mastery_key(user_id)) or {}
        items: List[Dict[str, Any]] = []
        for topic, value in raw.items():
            try:
                state = json.loads(value)
            except ValueError:
                continue
            items.append({
                "topic": topic,
                "correct": int(state.get("correct", 0)),
                "total": int(state.get("total", 0)),
                "mastery": float(state.get("mastery", 0)),
                "updated_at": state.get("updated_at", ""),
            })
        items.sort(key=lambda item: (item["mastery"], -item["total"]))
        return items

    async def summary(self, user_id: str) -> Dict[str, Any]:
        mastery = await self.mastery(user_id)
        mistakes = await self.list_mistakes(user_id, limit=5)
        total = sum(item["total"] for item in mastery)
        correct = sum(item["correct"] for item in mastery)
        weak: Optional[str] = mastery[0]["topic"] if mastery else None
        return {
            "mastery": mastery,
            "recent_mistakes": mistakes,
            "attempts": total,
            "accuracy": round(correct / total, 3) if total else 0.0,
            "weakest_topic": weak,
        }

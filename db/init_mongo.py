"""MongoDB 初始化脚本：knowledge_hub 库、应用用户与 document_content 集合。

原 mongosh 脚本的等价 Python 实现（pymongo），支持幂等重复执行：
  - 创建/切换 knowledge_hub 库
  - 创建应用用户 knowledge_hub_user（readWrite）
  - 创建集合 document_content
  - 建索引：documentId 唯一索引、deleted 普通索引

用法：
    python3 db/init_mongo.py

环境变量：
    MONGO_URI    连接串，默认与 docker-compose.yml 的 mongodb 服务一致：
                 mongodb://mongo_user:mongo_pass123@127.0.0.1:27017/?authSource=admin
                 （容器首启时 MONGO_INITDB_DATABASE=knowledge_hub 已在容器内建库）
    MONGO_APP_USER / MONGO_APP_PWD    应用账号密码，默认与原脚本一致
"""

import os

from pymongo import MongoClient
from pymongo.errors import CollectionInvalid, OperationFailure

DB_NAME = "knowledge_hub"
APP_USER = os.environ.get("MONGO_APP_USER", "knowledge_hub_user")
APP_PWD = os.environ.get("MONGO_APP_PWD", "knowledge_hub_password")
DEFAULT_URI = (
    "mongodb://mongo_user:mongo_pass123@127.0.0.1:27017/?authSource=admin"
)


def main() -> None:
    uri = os.environ.get("MONGO_URI", DEFAULT_URI)
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)

    db = client[DB_NAME]

    # 1) 创建应用用户（已存在则跳过）
    try:
        db.command(
            "createUser",
            APP_USER,
            pwd=APP_PWD,
            roles=[{"role": "readWrite", "db": DB_NAME}],
        )
        print(f"[OK] 已创建用户: {APP_USER} (readWrite@{DB_NAME})")
    except OperationFailure as e:
        # 用户已存在（如 code 51003 UserAlreadyExists），幂等跳过
        print(f"[SKIP] 用户 {APP_USER} 已存在，跳过创建 (code={e.code})")

    # 2) 创建集合（已存在则跳过）
    try:
        db.create_collection("document_content")
        print("[OK] 已创建集合: document_content")
    except CollectionInvalid:
        print("[SKIP] 集合 document_content 已存在，跳过创建")

    # 3) 索引
    # 文档正文：_id(ObjectId) ↔ kh_document.content_id，documentId ↔ kh_document.id
    coll = db["document_content"]
    coll.create_index([("documentId", 1)], unique=True, name="uniq_documentId")
    coll.create_index([("deleted", 1)], name="idx_deleted")
    print("[OK] 索引就绪: documentId(unique), deleted")

    client.close()
    print("MongoDB 初始化完成")


if __name__ == "__main__":
    main()

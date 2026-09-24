"""可见性范围与 Neo4j 过滤参数的纯逻辑。"""
from app.common.document_access import (
    empty_scope,
    neo4j_access_params,
    neo4j_document_access_where,
)


def test_empty_scope_is_public_only() -> None:
    scope = empty_scope()
    assert scope.unrestricted is False
    assert scope.user_id == "" and scope.team_ids == []
    assert neo4j_access_params(scope) == {
        "unrestricted": False,
        "accessUserId": "",
        "accessTeamIds": [],
    }


def test_none_scope_is_unrestricted() -> None:
    # scope=None 表示不过滤（生产链路请勿依赖，走 access_from_user / empty_scope）
    assert neo4j_access_params(None)["unrestricted"] is True


def test_neo4j_document_access_where_shape() -> None:
    where = neo4j_document_access_where("d")
    for token in ("$unrestricted", "d.authorId", "d.isPublic", "d.teamId"):
        assert token in where

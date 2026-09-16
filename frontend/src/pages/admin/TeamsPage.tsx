import { useEffect, useState } from "react";
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  message,
} from "antd";
import { teamApi, userApi } from "../../api";
import { ApiError } from "../../api/client";
import type { TeamItem } from "../../types";

interface MemberRow {
  userId: string;
  username: string;
  realName?: string;
  memberRole?: string;
}

export default function TeamsPage() {
  const [items, setItems] = useState<TeamItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState("");
  const [open, setOpen] = useState(false);
  const [membersFor, setMembersFor] = useState<TeamItem | null>(null);
  const [members, setMembers] = useState<MemberRow[]>([]);
  const [selectedUserId, setSelectedUserId] = useState<string>();
  const [userOptions, setUserOptions] = useState<
    { value: string; label: string }[]
  >([]);
  const [searching, setSearching] = useState(false);
  const [form] = Form.useForm();

  async function load(nextPage = page) {
    try {
      const res = await teamApi.page({
        page: nextPage,
        pageSize: 10,
        keyword: keyword.trim() || undefined,
      });
      setItems(res.items);
      setTotal(res.total);
      setPage(nextPage);
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "加载失败");
    }
  }

  async function loadMembers(teamId: string) {
    const rows = (await teamApi.members(teamId)) as MemberRow[];
    setMembers(rows);
    setItems((prev) =>
      prev.map((team) =>
        team.id === teamId ? { ...team, memberCount: rows.length } : team,
      ),
    );
    return rows;
  }

  async function searchUsers(query: string) {
    const kw = query.trim().toLowerCase();
    if (!kw) {
      setUserOptions([]);
      return;
    }
    setSearching(true);
    try {
      const res = await userApi.page({
        keyword: query.trim(),
        page: 1,
        pageSize: 20,
      });
      const taken = new Set(members.map((row) => row.userId));
      setUserOptions(
        res.items
          .filter(
            (user) =>
              !taken.has(user.id) && user.username.toLowerCase().includes(kw),
          )
          .map((user) => ({
            value: user.id,
            label: user.realName
              ? `${user.username}（${user.realName}）`
              : user.username,
          })),
      );
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "搜索用户失败");
    } finally {
      setSearching(false);
    }
  }

  useEffect(() => {
    void load(1);
  }, []);

  return (
    <div className="kh-page">
      <Space style={{ marginBottom: 16 }}>
        <Input
          placeholder="团队名称"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onPressEnter={() => void load(1)}
        />
        <Button onClick={() => void load(1)}>查询</Button>
        <Button type="primary" onClick={() => setOpen(true)}>
          新建团队
        </Button>
      </Space>
      <Table
        rowKey="id"
        dataSource={items}
        pagination={{
          current: page,
          pageSize: 10,
          total,
          onChange: (p) => void load(p),
        }}
        columns={[
          { title: "名称", dataIndex: "teamName" },
          { title: "编码", dataIndex: "teamCode" },
          { title: "说明", dataIndex: "description" },
          { title: "成员数", dataIndex: "memberCount" },
          {
            title: "操作",
            render: (_: unknown, row: TeamItem) => (
              <a
                onClick={async () => {
                  setMembersFor(row);
                  setSelectedUserId(undefined);
                  setUserOptions([]);
                  try {
                    await loadMembers(row.id);
                  } catch (error) {
                    message.error(
                      error instanceof ApiError
                        ? error.message
                        : "加载成员失败",
                    );
                  }
                }}
              >
                成员
              </a>
            ),
          },
        ]}
      />
      <Modal
        title="新建团队"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values: {
            teamName: string;
            teamCode?: string;
            description?: string;
          }) => {
            try {
              await teamApi.create(values);
              message.success("已创建");
              setOpen(false);
              form.resetFields();
              void load(1);
            } catch (error) {
              message.error(
                error instanceof ApiError ? error.message : "创建失败",
              );
            }
          }}
        >
          <Form.Item name="teamName" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="teamCode" label="编码">
            <Input />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={membersFor ? `成员：${membersFor.teamName}` : "成员"}
        open={Boolean(membersFor)}
        onCancel={() => setMembersFor(null)}
        footer={null}
        width={640}
      >
        <Space style={{ marginBottom: 12 }} wrap>
          <Select
            showSearch={{
              filterOption: false,
              onSearch: (value) => void searchUsers(value),
            }}
            allowClear
            placeholder="按用户名搜索"
            value={selectedUserId}
            options={userOptions}
            loading={searching}
            onChange={(value) => setSelectedUserId(value)}
            style={{ width: 280 }}
            notFoundContent={searching ? "搜索中…" : "输入用户名搜索"}
          />
          <Button
            type="primary"
            onClick={async () => {
              if (!membersFor || !selectedUserId) {
                message.warning("请先按用户名搜索并选择成员");
                return;
              }
              try {
                await teamApi.addMembers(membersFor.id, [selectedUserId]);
                await loadMembers(membersFor.id);
                setSelectedUserId(undefined);
                setUserOptions([]);
                message.success("已添加");
              } catch (error) {
                message.error(
                  error instanceof ApiError ? error.message : "添加失败",
                );
              }
            }}
          >
            添加
          </Button>
        </Space>
        <Table
          rowKey="userId"
          dataSource={members}
          pagination={false}
          columns={[
            { title: "用户名", dataIndex: "username" },
            { title: "姓名", dataIndex: "realName" },
            { title: "角色", dataIndex: "memberRole" },
            {
              title: "操作",
              width: 80,
              render: (_: unknown, row: MemberRow) => (
                <Popconfirm
                  title={`确定将 ${row.username} 移出团队？`}
                  okText="踢出"
                  okType="danger"
                  cancelText="取消"
                  onConfirm={async () => {
                    if (!membersFor) return;
                    try {
                      await teamApi.removeMembers(membersFor.id, [row.userId]);
                      await loadMembers(membersFor.id);
                      message.success("已移出");
                    } catch (error) {
                      message.error(
                        error instanceof ApiError ? error.message : "移出失败",
                      );
                    }
                  }}
                >
                  <a style={{ color: "#ff4d4f" }}>踢出</a>
                </Popconfirm>
              ),
            },
          ]}
        />
      </Modal>
    </div>
  );
}

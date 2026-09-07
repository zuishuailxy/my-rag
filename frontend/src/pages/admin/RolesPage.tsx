import { useEffect, useState } from "react";
import { Button, Form, Input, Modal, Table, Tree, message } from "antd";
import { permissionApi, roleApi } from "../../api";
import { ApiError } from "../../api/client";
import type { PermissionNode, RoleItem } from "../../types";

function toTree(
  nodes: PermissionNode[],
): { title: string; key: string; children?: ReturnType<typeof toTree> }[] {
  return nodes.map((n) => ({
    title: `${n.permissionName} (${n.permissionCode})`,
    key: n.id,
    children: n.children?.length ? toTree(n.children) : undefined,
  }));
}

export default function RolesPage() {
  const [items, setItems] = useState<RoleItem[]>([]);
  const [tree, setTree] = useState<PermissionNode[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [permRole, setPermRole] = useState<RoleItem | null>(null);
  const [checked, setChecked] = useState<string[]>([]);
  const [form] = Form.useForm();

  async function load() {
    try {
      setItems(await roleApi.list());
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "加载失败");
    }
  }

  useEffect(() => {
    void load();
    permissionApi
      .tree()
      .then(setTree)
      .catch(() => undefined);
  }, []);

  return (
    <div className="kh-page">
      <Button
        type="primary"
        style={{ marginBottom: 16 }}
        onClick={() => setCreateOpen(true)}
      >
        新建角色
      </Button>
      <Table
        rowKey="id"
        dataSource={items}
        columns={[
          { title: "名称", dataIndex: "roleName" },
          { title: "编码", dataIndex: "roleCode" },
          { title: "说明", dataIndex: "description" },
          {
            title: "操作",
            render: (_: unknown, row: RoleItem) => (
              <a
                onClick={async () => {
                  setPermRole(row);
                  try {
                    const res = await roleApi.permissions(row.id);
                    setChecked(res.permissionIds);
                  } catch (error) {
                    message.error(
                      error instanceof ApiError
                        ? error.message
                        : "读取权限失败",
                    );
                  }
                }}
              >
                权限
              </a>
            ),
          },
        ]}
      />
      <Modal
        title="新建角色"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values: {
            roleName: string;
            roleCode: string;
            description?: string;
          }) => {
            try {
              await roleApi.create(values);
              message.success("已创建");
              setCreateOpen(false);
              form.resetFields();
              void load();
            } catch (error) {
              message.error(
                error instanceof ApiError ? error.message : "创建失败",
              );
            }
          }}
        >
          <Form.Item name="roleName" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="roleCode" label="编码" rules={[{ required: true }]}>
            <Input placeholder="如 ROLE_EDITOR" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={permRole ? `权限：${permRole.roleName}` : "权限"}
        open={Boolean(permRole)}
        onCancel={() => setPermRole(null)}
        onOk={async () => {
          if (!permRole) return;
          try {
            await roleApi.assignPermissions(permRole.id, checked);
            message.success("已保存");
            setPermRole(null);
          } catch (error) {
            message.error(
              error instanceof ApiError ? error.message : "保存失败",
            );
          }
        }}
        width={560}
      >
        <Tree
          checkable
          checkedKeys={checked}
          onCheck={(keys) => {
            const list = Array.isArray(keys) ? keys : keys.checked;
            setChecked(list.map(String));
          }}
          treeData={toTree(tree)}
        />
      </Modal>
    </div>
  );
}

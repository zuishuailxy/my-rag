import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Button, Form, Input, Spin, Switch, message } from "antd";
import { documentApi } from "../api";
import { ApiError } from "../api/client";

export default function DocumentEditPage() {
  const { id } = useParams();
  const isNew = !id;
  const navigate = useNavigate();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(!isNew);

  useEffect(() => {
    if (!id) return;
    documentApi
      .get(id)
      .then((doc) => {
        form.setFieldsValue({
          title: doc.title,
          summary: doc.summary,
          tags: doc.tags,
          isPublic: doc.isPublic,
          content: doc.content,
        });
      })
      .catch((error) => {
        message.error(error instanceof ApiError ? error.message : "加载失败");
      })
      .finally(() => setLoading(false));
  }, [id, form]);

  if (loading) {
    return (
      <div className="kh-page">
        <Spin />
      </div>
    );
  }

  return (
    <div className="kh-page">
      <h2 style={{ marginTop: 0 }}>{isNew ? "新建文档" : "编辑文档"}</h2>
      <Form
        form={form}
        layout="vertical"
        initialValues={{ isPublic: true, content: "" }}
        onFinish={async (values: {
          title: string;
          summary?: string;
          tags?: string;
          isPublic: boolean;
          content: string;
        }) => {
          try {
            if (isNew) {
              const created = await documentApi.create({
                title: values.title,
                content: values.content,
                summary: values.summary || undefined,
                tags: values.tags || undefined,
                isPublic: values.isPublic,
                status: 0,
              });
              message.success("已保存为草稿");
              navigate(`/documents/${created.id}`);
            } else if (id) {
              await documentApi.update(id, {
                title: values.title,
                content: values.content,
                summary: values.summary || undefined,
                tags: values.tags || undefined,
                isPublic: values.isPublic,
              });
              message.success("已保存");
              navigate(`/documents/${id}`);
            }
          } catch (error) {
            message.error(
              error instanceof ApiError ? error.message : "保存失败",
            );
          }
        }}
      >
        <Form.Item name="title" label="标题" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="summary" label="摘要">
          <Input.TextArea rows={2} />
        </Form.Item>
        <Form.Item name="tags" label="标签">
          <Input placeholder="逗号分隔，如 SOP,发布" />
        </Form.Item>
        <Form.Item name="isPublic" label="公开" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item
          name="content"
          label="正文（Markdown）"
          rules={[{ required: true }]}
        >
          <Input.TextArea rows={18} />
        </Form.Item>
        <Button type="primary" htmlType="submit">
          保存
        </Button>
        <Button style={{ marginLeft: 8 }} onClick={() => navigate(-1)}>
          取消
        </Button>
      </Form>
    </div>
  );
}

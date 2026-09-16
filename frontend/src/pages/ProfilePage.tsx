import { useEffect, useState } from "react";
import {
  Button,
  Card,
  Col,
  Form,
  Input,
  Row,
  Statistic,
  Tag,
  message,
} from "antd";
import { teamApi, userApi } from "../api";
import { ApiError } from "../api/client";
import type { TeamItem, UserStats } from "../types";
import { updateUser, useAuth } from "../auth";

export default function ProfilePage() {
  const user = useAuth();
  const [stats, setStats] = useState<UserStats | null>(null);
  const [teams, setTeams] = useState<TeamItem[]>([]);
  const [profileForm] = Form.useForm();
  const [pwdForm] = Form.useForm();

  useEffect(() => {
    userApi
      .stats()
      .then(setStats)
      .catch(() => undefined);
    teamApi
      .mine()
      .then(setTeams)
      .catch(() => undefined);
    profileForm.setFieldsValue({
      realName: user?.realName,
      email: user?.email,
    });
  }, [user, profileForm]);

  return (
    <div className="kh-page">
      <Row gutter={16}>
        <Col span={8}>
          <Statistic title="我的文档" value={stats?.documentCount ?? 0} />
        </Col>
        <Col span={8}>
          <Statistic title="浏览" value={stats?.viewCount ?? 0} />
        </Col>
        <Col span={8}>
          <Statistic title="点赞" value={stats?.likeCount ?? 0} />
        </Col>
      </Row>
      <Card title="资料" style={{ marginTop: 24 }}>
        <Form
          form={profileForm}
          layout="vertical"
          style={{ maxWidth: 420 }}
          onFinish={async (values: { realName?: string; email?: string }) => {
            try {
              await userApi.updateMe(values);
              if (user) {
                updateUser({
                  ...user,
                  realName: values.realName,
                  email: values.email,
                });
              }
              message.success("已更新");
            } catch (error) {
              message.error(
                error instanceof ApiError ? error.message : "更新失败",
              );
            }
          }}
        >
          <Form.Item label="用户名">
            <Input disabled value={user?.username} />
          </Form.Item>
          <Form.Item label="所在团队">
            {teams.length ? (
              <div>
                {teams.map((team) => (
                  <Tag key={team.id}>{team.teamName}</Tag>
                ))}
              </div>
            ) : (
              <span style={{ color: "#8c8c8c" }}>暂未加入团队</span>
            )}
          </Form.Item>
          <Form.Item name="realName" label="姓名">
            <Input />
          </Form.Item>
          <Form.Item name="email" label="邮箱">
            <Input />
          </Form.Item>
          <Button type="primary" htmlType="submit">
            保存资料
          </Button>
        </Form>
      </Card>
      <Card title="修改密码" style={{ marginTop: 16 }}>
        <Form
          form={pwdForm}
          layout="vertical"
          style={{ maxWidth: 420 }}
          onFinish={async (values: {
            oldPassword: string;
            newPassword: string;
          }) => {
            try {
              await userApi.changePassword(
                values.oldPassword,
                values.newPassword,
              );
              message.success("密码已修改");
              pwdForm.resetFields();
            } catch (error) {
              message.error(
                error instanceof ApiError ? error.message : "修改失败",
              );
            }
          }}
        >
          <Form.Item
            name="oldPassword"
            label="原密码"
            rules={[{ required: true }]}
          >
            <Input.Password />
          </Form.Item>
          <Form.Item
            name="newPassword"
            label="新密码"
            rules={[{ required: true, min: 6 }]}
          >
            <Input.Password />
          </Form.Item>
          <Button type="primary" htmlType="submit">
            修改密码
          </Button>
        </Form>
      </Card>
    </div>
  );
}

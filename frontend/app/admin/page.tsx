"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import StatusCards from "@/components/admin/StatusCards";
import DataOverview from "@/components/admin/DataOverview";
import DebugConsole from "@/components/admin/DebugConsole";
import SchemaCard from "@/components/admin/SchemaCard";
import HistorySearch from "@/components/admin/HistorySearch";
import {
  api,
  getToken,
  type Health,
  type IngestStatus,
  type KbStats,
  type SchemaResponse,
  type User,
} from "@/lib/api";

/**
 * 后台管理页 /admin：集中展示系统状态、数据概览、调试控制台（建库日志）、
 * MySQL 表结构（可展开）与历史检索，并提供一个「返回对话」入口。
 *
 * 【为什么单独一页】
 *   主对话页要尽量干净（只留对话），把偏「管理/运维」的内容都收拢到这里；
 *   数据由本页自己拉取（health / kb / schema / ingest / me），与主页面互不影响。
 */

export default function AdminPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [kb, setKb] = useState<KbStats | null>(null);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [ingest, setIngest] = useState<IngestStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);

  /** 拉取后台所需数据；refresh=true 时带上 schema 强制刷新 */
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [h, k, s] = await Promise.all([api.health(), api.kbStats(), api.schema()]);
      setHealth(h);
      setKb(k);
      setSchema(s);
      setBanner(null);
    } catch (e) {
      setBanner(`无法连接后端 ${e instanceof Error ? e.message : String(e)}。请确认后端已在 8000 端口启动。`);
    } finally {
      setLoading(false);
    }
  }, []);

  /** 启动重建知识库（后端在后台跑，本页轮询进度） */
  const rebuild = useCallback(async () => {
    try {
      await api.startIngest();
      setIngest(await api.ingestStatus());
      setBanner(null);
    } catch (e) {
      setBanner(`启动建库失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }, []);

  useEffect(() => {
    void refresh();
    void (async () => {
      try {
        setIngest(await api.ingestStatus());
      } catch {
        /* 未开始建库或接口异常时忽略 */
      }
    })();
  }, [refresh]);

  // 建库进行中时轮询进度
  useEffect(() => {
    if (!ingest?.running) return;
    const timer = setInterval(async () => {
      try {
        const status = await api.ingestStatus();
        setIngest(status);
        if (!status.running) void refresh();
      } catch {
        /* 轮询失败忽略，下一轮继续 */
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [ingest?.running, refresh]);

  // 启动时若本地有令牌，拉一次当前用户，供「历史检索」判断登录态
  useEffect(() => {
    if (!getToken()) return;
    let alive = true;
    void (async () => {
      try {
        const res = await api.me();
        if (alive) setUser(res.user);
      } catch {
        /* 未登录 / 令牌失效：保持未登录 */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="admin">
      <div className="admin-header">
        <div className="admin-title">
          <span className="brand-logo">标</span>
          后台控制台
        </div>
        <Link className="admin-back" href="/">
          ← 返回对话
        </Link>
      </div>

      {banner && <div className="notice notice-danger" style={{ marginBottom: 14 }}>{banner}</div>}

      <div className="admin-grid">
        <div className="admin-card admin-span-all">
          <div className="admin-card-title">系统状态</div>
          <StatusCards health={health} kb={kb} schema={schema} />
        </div>

        <DataOverview kb={kb} schema={schema} />

        <DebugConsole ingest={ingest} loading={loading} onRebuild={() => void rebuild()} onRefresh={() => void refresh()} />

        <SchemaCard schema={schema} />

        <HistorySearch user={user} />
      </div>
    </div>
  );
}
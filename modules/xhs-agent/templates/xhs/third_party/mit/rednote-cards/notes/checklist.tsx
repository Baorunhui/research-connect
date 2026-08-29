import type { CardTemplate, CardData, PageRenderData } from "../base"

const defaultData: CardData = {
  title: "自律改变生活的 6 个小习惯",
  pages: [
    "<ol><li>早起后先喝一杯温水，唤醒身体代谢</li><li>每天阅读 30 分钟，积累复利知识</li><li>睡前远离手机，提升深度睡眠质量</li><li>每周运动 3 次，保持精力充沛</li><li>学会记账，清楚每一笔消费去向</li><li>定期断舍离，只留真正需要的东西</li></ol>",
  ],
  tags: ["自律", "生活方式", "好习惯"],
}

function ChecklistCard({ data }: { data: PageRenderData }) {
  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        background: "linear-gradient(160deg, #FFF5F5 0%, #FFF0F6 30%, #F0E4FF 65%, #E8F0FE 100%)",
        display: "flex",
        flexDirection: "column",
        padding: "52px 40px 36px",
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
        position: "relative",
        overflow: "hidden",
      }}
    >
      {/* === 装饰层 === */}
      <div
        style={{
          position: "absolute",
          top: -80,
          right: -80,
          width: 280,
          height: 280,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(255, 36, 66, 0.12) 0%, rgba(255, 36, 66, 0) 70%)",
          filter: "blur(20px)",
        }}
      />
      <div
        style={{
          position: "absolute",
          bottom: -60,
          left: -60,
          width: 240,
          height: 240,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(139, 92, 246, 0.1) 0%, rgba(139, 92, 246, 0) 70%)",
          filter: "blur(16px)",
        }}
      />
      <div
        style={{
          position: "absolute",
          top: "45%",
          right: -30,
          width: 160,
          height: 160,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(59, 130, 246, 0.07) 0%, transparent 70%)",
          filter: "blur(12px)",
        }}
      />
      <svg
        width="120"
        height="120"
        viewBox="0 0 120 120"
        fill="none"
        style={{ position: "absolute", top: 16, right: 16, opacity: 0.12 }}
      >
        <circle cx="90" cy="30" r="28" stroke="#FF2442" strokeWidth="1.5" />
        <circle cx="90" cy="30" r="18" stroke="#FF2442" strokeWidth="1" />
        <line x1="40" y1="0" x2="40" y2="80" stroke="#8B5CF6" strokeWidth="1" />
        <line x1="55" y1="10" x2="55" y2="60" stroke="#8B5CF6" strokeWidth="0.8" />
      </svg>
      <svg
        width="80"
        height="80"
        viewBox="0 0 80 80"
        fill="none"
        style={{ position: "absolute", bottom: 50, left: 12, opacity: 0.08 }}
      >
        <rect x="10" y="10" width="40" height="40" rx="8" stroke="#FF2442" strokeWidth="1.5" transform="rotate(15 30 30)" />
        <circle cx="55" cy="55" r="12" stroke="#8B5CF6" strokeWidth="1" />
      </svg>
      <div
        style={{
          position: "absolute",
          inset: 0,
          opacity: 0.03,
          backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E")`,
        }}
      />

      {/* === 内容区 === */}
      <div style={{ position: "relative", marginBottom: 28 }}>
        <div
          style={{
            position: "absolute",
            left: -6,
            bottom: 2,
            height: 14,
            width: "60%",
            background: "linear-gradient(90deg, rgba(255, 36, 66, 0.15), rgba(139, 92, 246, 0.08))",
            borderRadius: 4,
          }}
        />
        <h1
          style={{
            fontSize: 34,
            fontWeight: 800,
            lineHeight: 1.3,
            color: "#1A1A1A",
            position: "relative",
            letterSpacing: 0.5,
          }}
        >
          {data.title}
        </h1>
      </div>

      {/* 富文本内容 */}
      <div
        className="tpl-checklist-body"
        style={{
          flex: 1,
          position: "relative",
          overflow: "hidden",
          counterReset: `checklist-counter ${data.listStartIndex - 1}`,
        }}
        dangerouslySetInnerHTML={{ __html: data.content }}
      />

      {/* 标签 */}
      {data.tags.length > 0 && (
        <div style={{ display: "flex", gap: 8, marginTop: 24, flexWrap: "wrap" }}>
          {data.tags.map((tag, i) => (
            <span
              key={tag}
              style={{
                fontSize: 13,
                color: i % 2 === 0 ? "#E8254A" : "#7C3AED",
                background: i % 2 === 0 ? "rgba(255, 36, 66, 0.08)" : "rgba(139, 92, 246, 0.08)",
                padding: "5px 14px",
                borderRadius: 20,
                fontWeight: 500,
                border: `1px solid ${i % 2 === 0 ? "rgba(255, 36, 66, 0.12)" : "rgba(139, 92, 246, 0.12)"}`,
              }}
            >
              #{tag}
            </span>
          ))}
        </div>
      )}

      {/* 页码 */}
      {data.pageInfo && (
        <div
          style={{
            position: "absolute",
            bottom: 14,
            right: 20,
            fontSize: 11,
            color: "rgba(139, 92, 246, 0.45)",
            fontWeight: 600,
            letterSpacing: 1,
          }}
        >
          {data.pageInfo.current} / {data.pageInfo.total}
        </div>
      )}
    </div>
  )
}

export const checklistTemplate: CardTemplate = {
  id: "checklist",
  name: "干货清单",
  description: "毛玻璃卡片 + 多彩渐变，适合技巧分享、清单类内容",
  render: (data) => <ChecklistCard data={data} />,
  defaultData,
}
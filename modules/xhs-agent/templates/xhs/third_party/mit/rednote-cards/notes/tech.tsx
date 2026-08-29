import type { CardTemplate, CardData, PageRenderData } from "../base"

const defaultData: CardData = {
  title: "2025 年值得关注的 AI 工具",
  pages: [
    "<ol><li>ChatGPT — 最强通用对话 AI，支持联网与插件</li><li>Midjourney — AI 绘画天花板，出图质感一流</li><li>Cursor — AI 编程编辑器，写代码效率翻倍</li><li>NotebookLM — Google 出品的 AI 笔记助手</li><li>Runway — 视频生成领域的领跑者</li></ol>",
  ],
  tags: ["AI工具", "科技", "效率提升"],
}

function TechCard({ data }: { data: PageRenderData }) {
  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        background: "linear-gradient(165deg, #0A0E1A 0%, #0F1629 30%, #131B36 60%, #0D1225 100%)",
        display: "flex",
        flexDirection: "column",
        padding: "52px 40px 36px",
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", "Hiragino Sans GB", sans-serif',
        position: "relative",
        overflow: "hidden",
      }}
    >
      {/* === 装饰层 === */}
      {/* 网格线 */}
      <svg
        width="414"
        height="552"
        viewBox="0 0 414 552"
        fill="none"
        style={{ position: "absolute", inset: 0, opacity: 0.06 }}
      >
        {Array.from({ length: 12 }, (_, i) => (
          <line
            key={`h${i}`}
            x1="0"
            y1={i * 50}
            x2="414"
            y2={i * 50}
            stroke="#00D4FF"
            strokeWidth="0.5"
          />
        ))}
        {Array.from({ length: 9 }, (_, i) => (
          <line
            key={`v${i}`}
            x1={i * 50}
            y1="0"
            x2={i * 50}
            y2="552"
            stroke="#00D4FF"
            strokeWidth="0.5"
          />
        ))}
      </svg>

      {/* 主光晕 - 青色 */}
      <div
        style={{
          position: "absolute",
          top: -100,
          right: -60,
          width: 300,
          height: 300,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(0, 212, 255, 0.15) 0%, transparent 70%)",
          filter: "blur(40px)",
        }}
      />
      {/* 副光晕 - 紫色 */}
      <div
        style={{
          position: "absolute",
          bottom: -80,
          left: -40,
          width: 260,
          height: 260,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(99, 102, 241, 0.12) 0%, transparent 70%)",
          filter: "blur(30px)",
        }}
      />
      {/* 小光点 - 品红 */}
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: "20%",
          width: 120,
          height: 120,
          borderRadius: "50%",
          background: "radial-gradient(circle, rgba(236, 72, 153, 0.08) 0%, transparent 70%)",
          filter: "blur(20px)",
        }}
      />

      {/* 电路图案装饰 */}
      <svg
        width="140"
        height="140"
        viewBox="0 0 140 140"
        fill="none"
        style={{ position: "absolute", top: 12, right: 12, opacity: 0.15 }}
      >
        <circle cx="20" cy="20" r="3" fill="#00D4FF" />
        <line x1="20" y1="20" x2="60" y2="20" stroke="#00D4FF" strokeWidth="1" />
        <circle cx="60" cy="20" r="2" fill="#00D4FF" />
        <line x1="60" y1="20" x2="60" y2="60" stroke="#00D4FF" strokeWidth="1" />
        <circle cx="60" cy="60" r="3" fill="#6366F1" />
        <line x1="60" y1="60" x2="100" y2="60" stroke="#6366F1" strokeWidth="1" />
        <circle cx="100" cy="60" r="2" fill="#6366F1" />
        <line x1="100" y1="60" x2="100" y2="100" stroke="#6366F1" strokeWidth="1" />
        <circle cx="100" cy="100" r="3" fill="#00D4FF" />
        <line x1="20" y1="20" x2="20" y2="80" stroke="#00D4FF" strokeWidth="0.8" strokeDasharray="4 3" />
        <rect x="80" y="10" width="30" height="30" rx="4" stroke="#6366F1" strokeWidth="0.8" />
        <rect x="10" y="90" width="25" height="25" rx="3" stroke="#00D4FF" strokeWidth="0.8" />
      </svg>

      {/* 底部电路装饰 */}
      <svg
        width="100"
        height="60"
        viewBox="0 0 100 60"
        fill="none"
        style={{ position: "absolute", bottom: 40, left: 16, opacity: 0.1 }}
      >
        <circle cx="10" cy="30" r="3" fill="#EC4899" />
        <line x1="10" y1="30" x2="50" y2="30" stroke="#EC4899" strokeWidth="1" />
        <line x1="50" y1="30" x2="50" y2="10" stroke="#EC4899" strokeWidth="1" />
        <line x1="50" y1="10" x2="90" y2="10" stroke="#EC4899" strokeWidth="0.8" />
        <circle cx="90" cy="10" r="2" fill="#EC4899" />
        <line x1="50" y1="30" x2="90" y2="50" stroke="#6366F1" strokeWidth="0.8" strokeDasharray="3 2" />
      </svg>

      {/* 扫描线 */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          opacity: 0.03,
          backgroundImage:
            "repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0, 212, 255, 0.15) 2px, rgba(0, 212, 255, 0.15) 3px)",
        }}
      />

      {/* === 内容区 === */}
      {/* 标题 */}
      {data.title && (
        <div style={{ position: "relative", marginBottom: 28 }}>
          {/* 标题装饰线 */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 10,
            }}
          >
            <div
              style={{
                width: 4,
                height: 4,
                background: "#00D4FF",
                boxShadow: "0 0 8px rgba(0, 212, 255, 0.6)",
              }}
            />
            <div
              style={{
                flex: 1,
                height: 1,
                background: "linear-gradient(90deg, rgba(0, 212, 255, 0.4), transparent)",
              }}
            />
          </div>
          <h1
            style={{
              fontSize: 32,
              fontWeight: 800,
              lineHeight: 1.35,
              color: "#F0F4FF",
              letterSpacing: 0.5,
            }}
          >
            {data.title}
          </h1>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginTop: 10,
            }}
          >
            <div
              style={{
                width: 24,
                height: 1,
                background: "linear-gradient(90deg, rgba(0, 212, 255, 0.5), transparent)",
              }}
            />
            <div
              style={{
                width: 6,
                height: 6,
                border: "1px solid rgba(0, 212, 255, 0.4)",
                transform: "rotate(45deg)",
              }}
            />
            <div
              style={{
                width: 40,
                height: 1,
                background: "linear-gradient(90deg, rgba(99, 102, 241, 0.3), transparent)",
              }}
            />
          </div>
        </div>
      )}

      {/* 富文本内容 */}
      <div
        className="tpl-tech-body"
        style={{
          flex: 1,
          position: "relative",
          overflow: "hidden",
          counterReset: `tech-counter ${data.listStartIndex - 1}`,
        }}
        dangerouslySetInnerHTML={{ __html: data.content }}
      />

      {/* 标签 */}
      {data.tags.length > 0 && (
        <div style={{ display: "flex", gap: 8, marginTop: 24, flexWrap: "wrap" }}>
          {data.tags.map((tag, i) => {
            const colors = ["#00D4FF", "#6366F1", "#EC4899"]
            const c = colors[i % colors.length]
            return (
              <span
                key={tag}
                style={{
                  fontSize: 12,
                  color: c,
                  background: `${c}10`,
                  padding: "4px 14px",
                  borderRadius: 4,
                  fontWeight: 500,
                  border: `1px solid ${c}25`,
                  letterSpacing: 0.5,
                  fontFamily: '"SF Mono", "Fira Code", monospace',
                }}
              >
                #{tag}
              </span>
            )
          })}
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
            color: "rgba(0, 212, 255, 0.35)",
            fontWeight: 600,
            letterSpacing: 2,
            fontFamily: '"SF Mono", "Fira Code", monospace',
          }}
        >
          {String(data.pageInfo.current).padStart(2, "0")} / {String(data.pageInfo.total).padStart(2, "0")}
        </div>
      )}
    </div>
  )
}

export const techTemplate: CardTemplate = {
  id: "tech",
  name: "科技数码",
  description: "赛博暗色调 + 霓虹发光线条，适合科技、工具、数码类内容",
  render: (data) => <TechCard data={data} />,
  defaultData,
}

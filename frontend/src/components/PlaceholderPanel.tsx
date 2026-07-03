import type { CSSProperties } from "react";

type PlaceholderPanelProps = {
  title: string;
  label: string;
  className?: string;
  style?: CSSProperties;
  metadata?: Array<{ label: string; value: string }>;
};

export function PlaceholderPanel({ title, label, className = "", style, metadata = [] }: PlaceholderPanelProps) {
  return (
    <section className={`panel placeholder-panel ${className}`.trim()} style={style}>
      <p className="eyebrow">{label}</p>
      <h2>{title}</h2>
      {metadata.length > 0 ? (
        <dl className="placeholder-metadata">
          {metadata.map((item) => (
            <div key={`${item.label}:${item.value}`}>
              <dt>{item.label}</dt>
              <dd>{item.value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <div className="placeholder-surface" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
      )}
    </section>
  );
}

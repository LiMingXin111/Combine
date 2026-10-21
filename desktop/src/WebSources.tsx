type Source = {
  url?: string;
  title?: string;
  retrieved_at?: string;
  published_at?: string;
};

export default function WebSources({ sources = [] }: { sources?: Source[] }) {
  const pages = sources.filter((source, index) => {
    try {
      const url = new URL(source.url || "");
      return (
        ["http:", "https:"].includes(url.protocol) &&
        !url.username &&
        !url.password &&
        sources.findIndex((s) => s.url === source.url) === index
      );
    } catch {
      return false;
    }
  });
  if (!pages.length) return null;
  return (
    <details className="web-sources nodrag nowheel">
      <summary>
        {pages.length} web {pages.length === 1 ? "source" : "sources"}
      </summary>
      {pages.map((source) => (
        <div key={source.url}>
          <a href={source.url} target="_blank" rel="noreferrer noopener">
            {source.title || source.url}
          </a>
          <small>{new URL(source.url!).hostname}</small>
          {source.retrieved_at && (
            <small>
              Retrieved{" "}
              {new Date(source.retrieved_at).toLocaleString("en-GB", {
                timeZone: "UTC",
              })}{" "}
              UTC
            </small>
          )}
          {source.published_at && (
            <small>Published {source.published_at}</small>
          )}
        </div>
      ))}
    </details>
  );
}

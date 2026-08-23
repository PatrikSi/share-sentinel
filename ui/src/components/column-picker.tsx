type ColumnOption<T extends string> = {
  key: T;
  label: string;
};

type ColumnPickerProps<T extends string> = {
  options: ColumnOption<T>[];
  selected: T[];
  onToggle: (column: T) => void;
  onMove: (column: T, direction: -1 | 1) => void;
  onReset: () => void;
};

export function ColumnPicker<T extends string>({ options, selected, onToggle, onMove, onReset }: ColumnPickerProps<T>) {
  const selectedSet = new Set(selected);
  const ordered = [
    ...selected.map((key) => options.find((option) => option.key === key)).filter((option): option is ColumnOption<T> => !!option),
    ...options.filter((option) => !selectedSet.has(option.key)),
  ];

  return (
    <details className="inventory-popover">
      <summary className="inventory-toolbar-button">
        Columns <span className="inventory-toolbar-count">{selected.length}</span>
      </summary>
      <div className="inventory-popover-panel inventory-column-panel">
        <div className="inventory-popover-heading">
          <div>
            <p className="inventory-popover-title">Visible columns</p>
            <p className="inventory-popover-copy">Choose fields and set their table order.</p>
          </div>
          <button className="inventory-text-button" onClick={onReset} type="button">
            Reset
          </button>
        </div>

        <div className="inventory-column-list">
          {ordered.map((option) => {
            const index = selected.indexOf(option.key);
            const visible = index >= 0;
            return (
              <div className="inventory-column-option" key={option.key}>
                <label>
                  <input checked={visible} onChange={() => onToggle(option.key)} type="checkbox" />
                  <span>{option.label}</span>
                </label>
                {visible ? (
                  <div className="inventory-column-order" aria-label={`Reorder ${option.label}`}>
                    <button
                      aria-label={`Move ${option.label} left`}
                      disabled={index === 0}
                      onClick={() => onMove(option.key, -1)}
                      title="Move left"
                      type="button"
                    >
                      ←
                    </button>
                    <button
                      aria-label={`Move ${option.label} right`}
                      disabled={index === selected.length - 1}
                      onClick={() => onMove(option.key, 1)}
                      title="Move right"
                      type="button"
                    >
                      →
                    </button>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    </details>
  );
}

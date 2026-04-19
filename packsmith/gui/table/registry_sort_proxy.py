from PySide6.QtCore import Qt, QSortFilterProxyModel


class RegistrySortProxy(QSortFilterProxyModel):
    """Sort proxy that respects tag data types.
    Strings/enums sort alphabetically, numbers sort numerically,
    bools sort False before True, and empty values always sink to the bottom."""

    def __init__(self, tag_store, tag_columns: list[str]):
        super().__init__()
        self._tag_store = tag_store
        self._tag_columns = tag_columns
        self._fixed_col_count = 2  # ID, Display Name

    def lessThan(self, left, right):
        col = left.column()
        left_val = self.sourceModel().data(left, Qt.DisplayRole)
        right_val = self.sourceModel().data(right, Qt.DisplayRole)

        # Empty values always sink to bottom regardless of sort direction
        if left_val == "" and right_val == "":
            return False
        if left_val == "":
            return self.sortOrder() == Qt.DescendingOrder
        if right_val == "":
            return self.sortOrder() == Qt.AscendingOrder

        # Fixed columns (ID, Display Name) — always string sort
        if col < self._fixed_col_count:
            return left_val.lower() < right_val.lower()

        # Tag columns — sort based on tag type
        tag_name = self._tag_columns[col - self._fixed_col_count]
        defn = self._tag_store.definition(tag_name)
        if not defn:
            return left_val.lower() < right_val.lower()

        tag_type = defn["type"]

        if tag_type == "number":
            try:
                return float(left_val) < float(right_val)
            except ValueError:
                return left_val.lower() < right_val.lower()

        if tag_type == "bool":
            return left_val.lower() == "false" and right_val.lower() == "true"

        # string and enum — alphabetical
        return left_val.lower() < right_val.lower()

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Vertical and role == Qt.DisplayRole:
            return str(section + 1)
        return super().headerData(section, orientation, role)

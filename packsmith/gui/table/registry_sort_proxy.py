from PySide6.QtCore import Qt, QSortFilterProxyModel


class RegistrySortProxy(QSortFilterProxyModel):
    """Sort proxy that respects a column's data type.

    The source model is query-driven, so column types come straight from it
    (``tag_type_for_column`` — bool/string/enum/number, or None for id/mod/attribute,
    which sort as strings). Numbers sort numerically, bools False-before-True, everything
    else alphabetically, and empty values always sink to the bottom.
    """

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

        col_type = self.sourceModel().tag_type_for_column(col)

        if col_type == "number":
            try:
                return float(left_val) < float(right_val)
            except ValueError:
                return left_val.lower() < right_val.lower()

        if col_type == "bool":
            return left_val.lower() == "false" and right_val.lower() == "true"

        # string, enum, and intrinsic (id/mod/attribute) — alphabetical
        return left_val.lower() < right_val.lower()

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Vertical and role == Qt.DisplayRole:
            return str(section + 1)
        return super().headerData(section, orientation, role)

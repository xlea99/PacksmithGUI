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

        if col_type == "enum":
            # Enum values carry a DECLARATION order that is semantic — `tier` means
            # early < mid < late < end, which alphabetical sorting mangles into
            # early/end/late/mid. Sort by position in the definition instead.
            defn = self.sourceModel().tag_definition_for_column(col)
            values = defn.get("values", []) if defn else []
            if left_val in values and right_val in values:
                return values.index(left_val) < values.index(right_val)
            # A value outside the definition (an orphaned assignment) — fall through.

        # string and intrinsic (id/mod/attribute) — alphabetical
        return left_val.lower() < right_val.lower()

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Vertical and role == Qt.DisplayRole:
            return str(section + 1)
        return super().headerData(section, orientation, role)

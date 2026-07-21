const MIN_TABLE_PAGE_SIZE = 5
const MAX_TABLE_PAGE_SIZE = 1000
// Special value: load all rows in one page (must stay within backend ParsePagination cap).
export const TABLE_PAGE_SIZE_ALL = 1000

export const DEFAULT_TABLE_PAGE_SIZE = 20
export const DEFAULT_TABLE_PAGE_SIZE_OPTIONS = [10, 20, 50, 100, 200, 500, TABLE_PAGE_SIZE_ALL]

const sanitizePageSize = (value: unknown): number | null => {
  const size = Number(value)
  if (!Number.isInteger(size)) return null
  if (size < MIN_TABLE_PAGE_SIZE || size > MAX_TABLE_PAGE_SIZE) return null
  return size
}

const parsePageSizeForSelection = (value: unknown): number | null => {
  const size = Number(value)
  if (!Number.isInteger(size)) return null
  if (size < MIN_TABLE_PAGE_SIZE) return null
  // Cap to backend max instead of rejecting large values.
  if (size > MAX_TABLE_PAGE_SIZE) return MAX_TABLE_PAGE_SIZE
  return size
}

const getInjectedAppConfig = () => {
  if (typeof window === 'undefined') return null
  return window.__APP_CONFIG__ ?? null
}

const getSanitizedConfiguredOptions = (): number[] => {
  const configured = getInjectedAppConfig()?.table_page_size_options
  if (!Array.isArray(configured)) return []

  return Array.from(
    new Set(
      configured
        .map((value) => sanitizePageSize(value))
        .filter((value): value is number => value !== null)
    )
  ).sort((a, b) => a - b)
}

const normalizePageSizeToOptions = (value: number, options: number[]): number => {
  // Exact match first so "All (1000)" stays selectable even if not the next round-up.
  if (options.includes(value)) return value
  for (const option of options) {
    if (option >= value) {
      return option
    }
  }
  return options[options.length - 1]
}

export const getConfiguredTableDefaultPageSize = (): number => {
  const configured = sanitizePageSize(getInjectedAppConfig()?.table_default_page_size)
  if (configured === null) {
    return DEFAULT_TABLE_PAGE_SIZE
  }
  return configured
}

export const getConfiguredTablePageSizeOptions = (): number[] => {
  const unique = getSanitizedConfiguredOptions()
  if (unique.length === 0) {
    return [...DEFAULT_TABLE_PAGE_SIZE_OPTIONS]
  }

  // Always expose "all" when options are customized, as long as backend allows it.
  if (!unique.includes(TABLE_PAGE_SIZE_ALL) && TABLE_PAGE_SIZE_ALL <= MAX_TABLE_PAGE_SIZE) {
    unique.push(TABLE_PAGE_SIZE_ALL)
    unique.sort((a, b) => a - b)
  }

  return unique.length > 0 ? unique : [...DEFAULT_TABLE_PAGE_SIZE_OPTIONS]
}

export const normalizeTablePageSize = (value: unknown): number => {
  const normalized = parsePageSizeForSelection(value)
  const defaultSize = getConfiguredTableDefaultPageSize()
  const options = getConfiguredTablePageSizeOptions()
  if (normalized !== null) {
    return normalizePageSizeToOptions(normalized, options)
  }
  return normalizePageSizeToOptions(defaultSize, options)
}

export const isTablePageSizeAll = (value: unknown): boolean => {
  return Number(value) === TABLE_PAGE_SIZE_ALL
}

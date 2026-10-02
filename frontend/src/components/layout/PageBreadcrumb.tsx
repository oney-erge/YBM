import { Fragment } from "react"
import { Link } from "react-router"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"

/**
 * A consistent "where am I, and how do I get back" for every sub-page
 * (docs/archive/UI_UX_AUDIT.md Phase 12) - Memory/Skills/Tools under the Agent hub
 * and a task's trace under Tasks previously had either no way back or an
 * ad hoc, one-off link. The last item is always the current page (no
 * link); every item before it links up the hierarchy.
 */
export function PageBreadcrumb({ items }: { items: { label: string; to?: string }[] }) {
  return (
    <Breadcrumb className="mb-1">
      <BreadcrumbList>
        {items.map((item, index) => {
          const isLast = index === items.length - 1
          return (
            <Fragment key={`${item.label}-${index}`}>
              {index > 0 && <BreadcrumbSeparator />}
              <BreadcrumbItem>
                {isLast || !item.to ? (
                  <BreadcrumbPage className="max-w-60 truncate">{item.label}</BreadcrumbPage>
                ) : (
                  <BreadcrumbLink render={<Link to={item.to} />}>{item.label}</BreadcrumbLink>
                )}
              </BreadcrumbItem>
            </Fragment>
          )
        })}
      </BreadcrumbList>
    </Breadcrumb>
  )
}

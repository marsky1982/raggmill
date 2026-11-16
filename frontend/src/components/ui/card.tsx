import * as React from "react";
import { cn } from "@/lib/utils";

/** 与 index.css 的 `.card` 等价：bg-card + 描边 + 圆角。
 *  内边距/分割线由调用方 className 控制（现有页面都直接把 p-5、divide-y 挂在卡片根上），
 *  因此不套 CardContent 包装层，避免多一层 div 改变 grid / space-y 的布局。 */
function Card({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card"
      className={cn("rounded-lg border border-border bg-card", className)}
      {...props}
    />
  );
}

/** 列表类卡片的标题行（原为 px-5 py-3 text-sm font-medium 的裸 div） */
function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-header"
      className={cn("flex items-center gap-2", className)}
      {...props}
    />
  );
}

export { Card, CardHeader };

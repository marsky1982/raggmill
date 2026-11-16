import { Button } from "@/components/ui/button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

export type ExportFormat = "csv" | "jsonl";
export type ExportScope = "all" | "adopted";

/** 导出下拉：三个固定组合。请求与成功提示由页面持有（导出会落文件、要统一提示）。 */
export default function ExportMenu({ disabled, onExport }: {
  disabled?: boolean;
  onExport: (format: ExportFormat, scope: ExportScope) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="xs" disabled={disabled}>导出</Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-44">
        <DropdownMenuItem disabled={disabled} onSelect={() => onExport("csv", "all")}>CSV · 全部</DropdownMenuItem>
        <DropdownMenuItem disabled={disabled} onSelect={() => onExport("jsonl", "all")}>JSONL · 全部</DropdownMenuItem>
        <DropdownMenuItem disabled={disabled} onSelect={() => onExport("jsonl", "adopted")}>JSONL · 仅采纳</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

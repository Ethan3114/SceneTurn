export type CanvasColorTheme = "light" | "dark";
export type CanvasBackgroundMode = "dots" | "lines" | "blank";

export const canvasThemes = {
    light: {
        canvas: {
            background: "#f4f2ed",
            dot: "rgba(68,64,60,.28)",
            line: "rgba(68,64,60,.12)",
            selectionStroke: "#1c1917",
            selectionFill: "rgba(28,25,23,.06)",
        },
        node: {
            label: "#57534e",
            fill: "#e7e5df",
            panel: "#fbfaf7",
            stroke: "#d6d3ca",
            activeStroke: "#1c1917",
            placeholder: "#8a8479",
            text: "#292524",
            muted: "#78716c",
            faint: "#a8a29e",
            port: "rgba(41,37,36,.4)",
            connection: "#6d7179",
        },
        toolbar: {
            panel: "rgba(251,250,247,.96)",
            border: "#d6d3ca",
            item: "#57534e",
            itemHover: "#e7e5df",
            activeBg: "#e7e5df",
            activeText: "#292524",
        },
    },
    dark: {
        canvas: {
            background: "#101113",
            dot: "#3a3d45",
            line: "#282b31",
            selectionStroke: "#a9baff",
            selectionFill: "#a9baff1a",
        },
        node: {
            label: "#919191",
            fill: "#171717",
            panel: "#171717",
            stroke: "rgba(255,255,255,.08)",
            activeStroke: "#a9baff",
            placeholder: "#a5a7b1",
            text: "#f2f3f6",
            muted: "#919191",
            faint: "#797d88",
            port: "rgba(255,255,255,.30)",
            connection: "#86909c",
        },
        toolbar: {
            panel: "#24262b",
            border: "#3a3d45",
            item: "#b7b9c3",
            itemHover: "#202226",
            activeBg: "#343742",
            activeText: "#f2f3f6",
        },
    },
} as const;

export type CanvasTheme = (typeof canvasThemes)[CanvasColorTheme];

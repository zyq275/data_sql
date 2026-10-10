/**
 * ECharts 的按需引入与全局注册。
 *
 * 不用 `import * as echarts from 'echarts'`：那会把全部图表类型与组件（地图、关系图、
 * 3D、工具栏……）一起打进包里，而本项目只用三种图。按需注册后打包体积小一个量级，
 * 首屏少等的那一秒是实打实的。
 *
 * 代价是**用到的组件必须在这里注册**，漏了不会报错，只表现为"图不显示"或
 * "图例不见了"。所以新加图表类型时改这个文件，不要在使用处单独 import ——
 * 两处分别注册的话，谁漏了都不容易发现。
 */

import { BarChart, LineChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  TitleComponent,
  CanvasRenderer,
])

export { echarts }

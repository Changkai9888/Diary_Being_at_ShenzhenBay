let currentDate = new Date();
let selectedDate = null;

// 初始化日历（修改后）
async function initCalendar() {
    const today = new Date();
    const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
    
    await loadCalendar(today.getFullYear(), today.getMonth() + 1);
    selectDate(todayStr);
}

// 动态加载日历数据
async function loadCalendar(year, month) {
    try {
        const response = await fetch(`/get_calendar?year=${year}&month=${month}`);
        const data = await response.json();
        
        document.getElementById('monthTitle').textContent = data.month_title;
        renderCalendar(data.weeks);
    } catch (error) {
        console.error('日历加载失败:', error);
    }
}


// 渲染日历（修改后）
function renderCalendar(weeks) {
    const tbody = document.getElementById('calendarBody');
    tbody.innerHTML = weeks.map(week => `
        <tr>${week.map(day => {
            const dayDate = day.date.split('T')[0];  // 剥离时间部分
            return `
            <td onclick="selectDate('${dayDate}')" 
                class="${dayDate === selectedDate ? 'active-day' : ''} 
                       ${day.is_today ? 'today-highlight' : ''}">
                <div class="solar-day">${day.solar}</div>
                <div class="lunar-day">${day.lunar}</div>
            </td>`;
        }).join('')}</tr>
    `).join('');
}

// 选择日期并加载日志
async function selectDate(dateStr) {
    try {
        selectedDate = dateStr;
        const response = await fetch(`/get_log?date=${dateStr}`);
        const log = await response.json();
        
        // 更新日志标题
        document.getElementById('logTitle').textContent = 
            `${dateStr.replace(/-/g, '年', 1).replace('-', '月')}日 日志`;
        
        // 填充表单数据
        ['天气', '心情', '醒来状态','HP','San','起床体重','腰围','用药与健康建议', '计划与摘要', '上午', '下午', '晚上', '几天没做试验','心得备注', '待提升属性'].forEach(field => {
            document.querySelector(`[name=${field}]`).value = log[field] || '';
        });
        
        // 重新渲染日历高亮
        loadCalendar(new Date(dateStr).getFullYear(), 
                    new Date(dateStr).getMonth() + 1);
    } catch (error) {
        console.error('日志加载失败:', error);
    }
}

// 保存日志
async function saveLog(e) {
    e.preventDefault();
    const formData = {
        date: selectedDate,
        天气: e.target.天气.value,
        心情: e.target.心情.value,
        醒来状态: e.target.醒来状态.value,
        HP: e.target.HP.value,
        San: e.target.San.value,
        起床体重: e.target.起床体重.value,
        腰围: e.target.腰围.value,
        用药与健康建议: e.target.用药与健康建议.value,
        计划与摘要: e.target.计划与摘要.value,
        上午: e.target.上午.value,
        下午: e.target.下午.value,
        晚上: e.target.晚上.value,
        几天没做试验: e.target.几天没做试验.value,
        心得备注: e.target.心得备注.value,
        待提升属性: e.target.待提升属性.value,
    };

    try {
        await fetch('/save_log', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(formData)
        });
        alert('日志保存成功！');
    } catch (error) {
        console.error('保存失败:', error);
        alert('保存失败，请重试！');
    }
}

// 月份切换
function changeMonth(offset) {
    currentDate.setMonth(currentDate.getMonth() + offset);
    loadCalendar(currentDate.getFullYear(), 
                currentDate.getMonth() + 1);
}

// 初始化
document.addEventListener('DOMContentLoaded', initCalendar);

//AI分析，获取按钮元素
const aiButton = document.getElementById('aiButton');
aiButton.addEventListener('click', function() {// 为按钮添加点击事件监听器
    // 弹出警告框
    alert('AI 分析功能，目前尚未实现，敬请期待！');
});
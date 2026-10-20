import pandas as pd
from datetime import datetime

class LogManager:
    @staticmethod
    def get_filename(date_str):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{dt.year}.xlsx"

    @staticmethod
    def load_log(date_str):
        try:
            df = pd.read_excel(f"data/{LogManager.get_filename(date_str)}")
            log = df[df['date'] == date_str].iloc[0].to_dict()
        except Exception as e:
            log = {'date': date_str, '天气': '', '心情': '','醒来状态': '','HP': '','San': '','起床体重': '','腰围': '','用药与健康建议': '', '计划与摘要': '', '上午': '', '下午': '', '晚上': '', '几天没做试验': '','心得备注': '', '待提升属性': ''}
        
        return {k: (v if pd.notnull(v) else '') for k,v in log.items()}

    @staticmethod
    def save_log(data):
        filename = LogManager.get_filename(data['date'])
        filepath = f"data/{filename}"
        
        try:
            df = pd.read_excel(filepath)
        except FileNotFoundError:
            df = pd.DataFrame(columns=['date','天气','心情', '醒来状态','HP','San','起床体重','腰围','用药与健康建议', '计划与摘要', '上午', '下午', '晚上', '几天没做试验','心得备注', '待提升属性'])
        
        new_df = pd.DataFrame([data])
        df = pd.concat([df, new_df]).drop_duplicates('date', keep='last')
        df.to_excel(filepath, index=False)

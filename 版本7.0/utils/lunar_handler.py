from lunardate import LunarDate
from datetime import date

class LunarConverter:
    @staticmethod
    def solar_to_lunar(year, month, day):
        try:
            lunar = LunarDate.fromSolarDate(year, month, day)
            return {
                'cn_month': lunar.month,
                'cn_day': LunarConverter._day_to_cn(lunar.day),
                'is_leap': lunar.isLeapMonth
            }
        except Exception as e:
            return {'cn_day': '初一', 'is_leap': False}

    @staticmethod
    def _day_to_cn(day):
        cn_days = ['初一','初二','初三','初四','初五','初六','初七','初八','初九','初十',
                  '十一','十二','十三','十四','十五','十六','十七','十八','十九','二十',
                  '廿一','廿二','廿三','廿四','廿五','廿六','廿七','廿八','廿九','三十']
        return cn_days[day-1] if 1 <= day <= 30 else ''

    @staticmethod
    def is_today(y, m, d):
        today = date.today()
        return y == today.year and m == today.month and d == today.day
